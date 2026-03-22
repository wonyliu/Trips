const express = require("express");
const { spawn, execSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const HUB_PORT = Number(process.env.SERVICE_HUB_PORT || 7799);
const PRICE_AGENT_URL = process.env.PRICE_AGENT_URL || "http://127.0.0.1:7788/health";
const WORKDIR = process.cwd();
const SPOT_OVERRIDE_FILE = path.join(WORKDIR, "trip-spot-overrides.json");
const AMAP_WEB_KEY = String(process.env.AMAP_WEB_KEY || process.env.AMAP_KEY || "").trim();
const GEOCODE_CACHE_TTL_MS = 10 * 60 * 1000;
const WEATHER_CACHE_TTL_MS = 2 * 60 * 1000;
const WEATHER_FETCH_TIMEOUT_MS = 3500;
const WEATHER_OUTAGE_COOLDOWN_MS = 90 * 1000;
const geocodeCache = new Map();
const weatherCache = new Map();
let geocodeRateLimitedUntil = 0;
let weatherUnavailableUntil = 0;

function normalizeCoverMap(raw) {
  if (!raw || typeof raw !== "object") return {};
  const out = {};
  Object.entries(raw).forEach(([k, v]) => {
    const key = Number(k);
    const value = String(v || "").trim();
    if (!Number.isFinite(key) || !value) return;
    out[String(key)] = value;
  });
  return out;
}

function normalizeGalleryMap(raw) {
  if (!raw || typeof raw !== "object") return {};
  const out = {};
  Object.entries(raw).forEach(([k, v]) => {
    const key = Number(k);
    if (!Number.isFinite(key) || !Array.isArray(v)) return;
    const arr = [];
    const seen = new Set();
    v.forEach((x) => {
      const url = String(x || "").trim();
      if (!url || seen.has(url)) return;
      seen.add(url);
      arr.push(url);
    });
    if (arr.length) out[String(key)] = arr.slice(0, 30);
  });
  return out;
}

function readSpotOverridesFromDisk() {
  try {
    if (!fs.existsSync(SPOT_OVERRIDE_FILE)) {
      return { cover: {}, gallery: {}, updatedAt: 0 };
    }
    const raw = fs.readFileSync(SPOT_OVERRIDE_FILE, "utf8");
    const parsed = JSON.parse(raw);
    return {
      cover: normalizeCoverMap(parsed?.cover),
      gallery: normalizeGalleryMap(parsed?.gallery),
      updatedAt: Number(parsed?.updatedAt || 0) || 0
    };
  } catch (_err) {
    return { cover: {}, gallery: {}, updatedAt: 0 };
  }
}

function writeSpotOverridesToDisk(payload) {
  const normalized = {
    cover: normalizeCoverMap(payload?.cover),
    gallery: normalizeGalleryMap(payload?.gallery),
    updatedAt: Date.now()
  };
  fs.writeFileSync(SPOT_OVERRIDE_FILE, JSON.stringify(normalized, null, 2), "utf8");
  return normalized;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function getGeocodeCacheKey(query, limit, provider = "nominatim") {
  return `${String(provider || "nominatim").trim().toLowerCase()}__${String(query || "").trim().toLowerCase()}__${Number(limit) || 1}`;
}

function parseAmapLocation(rawLocation) {
  const raw = String(rawLocation || "").trim();
  const parts = raw.split(",");
  if (parts.length !== 2) return null;
  const lng = Number(parts[0]);
  const lat = Number(parts[1]);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  return { lat, lng };
}

function normalizeAmapTips(tips, query, limit) {
  const out = [];
  const seen = new Set();
  const rows = Array.isArray(tips) ? tips : [];
  for (const tip of rows) {
    const point = parseAmapLocation(tip?.location);
    if (!point) continue;
    const dedupeKey = `${point.lat.toFixed(6)},${point.lng.toFixed(6)}`;
    if (seen.has(dedupeKey)) continue;
    seen.add(dedupeKey);
    const nameParts = [
      String(tip?.name || "").trim(),
      String(tip?.district || "").trim(),
      String(tip?.address || "").trim()
    ].filter(Boolean);
    out.push({
      lat: point.lat,
      lng: point.lng,
      displayName: nameParts.length ? nameParts.join(" · ") : String(query || "").trim()
    });
    if (out.length >= limit) break;
  }
  return out;
}

function getGeocodeProvider() {
  return AMAP_WEB_KEY ? "amap" : "nominatim";
}

function getWeatherCacheKey(lat, lng) {
  return `${Number(lat).toFixed(4)},${Number(lng).toFixed(4)}`;
}

async function isPriceAgentUp() {
  try {
    const resp = await fetch(PRICE_AGENT_URL, { method: "GET" });
    return resp.ok;
  } catch (_err) {
    return false;
  }
}

function startPriceAgentMobileDetached() {
  const child = spawn(process.execPath, ["price-agent.js"], {
    cwd: WORKDIR,
    env: { ...process.env, PRICE_AGENT_SOURCE: "mobile" },
    detached: true,
    windowsHide: false,
    stdio: "ignore"
  });
  child.unref();
}

function killPriceAgentProcesses() {
  const killCmd = "Get-CimInstance Win32_Process | Where-Object { `$_.Name -match '^node(\\\\.exe)?$' -and `$_.CommandLine -match 'price-agent\\\\.js' } | ForEach-Object { Stop-Process -Id `$_.ProcessId -Force -ErrorAction SilentlyContinue }";
  try {
    execSync(`powershell -NoProfile -ExecutionPolicy Bypass -Command "${killCmd}"`, { stdio: "ignore" });
  } catch (_err) {
    // ignore kill failures; restart path still tries to start a fresh process
  }
}

async function restartAllServersSafe(timeoutMs = 30000) {
  // Keep service-hub alive to avoid self-restart permission issues (spawn EPERM on some hosts).
  killPriceAgentProcesses();
  await sleep(700);
  const result = await ensurePriceAgentUp(timeoutMs);
  return {
    ok: !!result.ok,
    status: result.ok ? "restarted" : "restart_timeout",
    serviceHub: "kept_running",
    priceAgent: result.status,
    message: result.ok
      ? "price-agent-mobile restarted; service-hub kept running"
      : "price-agent-mobile restart timed out; service-hub kept running"
  };
}

async function ensurePriceAgentUp(timeoutMs = 30000) {
  if (await isPriceAgentUp()) return { ok: true, status: "already_running" };
  startPriceAgentMobileDetached();
  const deadline = Date.now() + Math.max(3000, timeoutMs);
  while (Date.now() < deadline) {
    if (await isPriceAgentUp()) return { ok: true, status: "started" };
    await sleep(700);
  }
  return { ok: false, status: "start_timeout" };
}

async function main() {
  const app = express();
  app.use(express.json({ limit: "128kb" }));
  app.use((req, res, next) => {
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type");
    if (req.method === "OPTIONS") return res.status(204).end();
    next();
  });
  app.use((req, res, next) => {
    const p = String(req.path || "").toLowerCase();
    if (p === "/" || p.endsWith(".html") || p.endsWith(".js") || p.endsWith(".css")) {
      res.setHeader("Cache-Control", "no-store");
    }
    next();
  });
  app.get("/", (_req, res) => {
    res.sendFile(path.join(WORKDIR, "index.html"));
  });
  app.use(express.static(WORKDIR, { extensions: ["html"] }));

  app.get("/health", async (_req, res) => {
    const up = await isPriceAgentUp();
    res.json({
      ok: true,
      hub: "service-hub",
      port: HUB_PORT,
      cwd: WORKDIR,
      priceAgentUp: up,
      geocodeProvider: getGeocodeProvider(),
      amapKeyLoaded: !!AMAP_WEB_KEY
    });
  });

  app.post("/ensure/price-agent-mobile", async (req, res) => {
    const timeoutMs = Number(req.body?.timeoutMs || 30000);
    const result = await ensurePriceAgentUp(timeoutMs);
    if (!result.ok) return res.status(500).json(result);
    return res.json(result);
  });

  app.post("/restart/all", async (req, res) => {
    const timeoutMs = Number(req.body?.timeoutMs || 30000);
    const result = await restartAllServersSafe(timeoutMs);
    return res.json(result);
  });

  app.get("/api/spot-overrides", (_req, res) => {
    const data = readSpotOverridesFromDisk();
    return res.json({ ok: true, ...data });
  });

  app.post("/api/spot-overrides", (req, res) => {
    try {
      const current = readSpotOverridesFromDisk();
      const next = {
        cover: req.body?.cover && typeof req.body.cover === "object" ? req.body.cover : current.cover,
        gallery: req.body?.gallery && typeof req.body.gallery === "object" ? req.body.gallery : current.gallery
      };
      const saved = writeSpotOverridesToDisk(next);
      return res.json({ ok: true, ...saved });
    } catch (err) {
      return res.status(500).json({ ok: false, message: String(err?.message || err) });
    }
  });

  app.get("/api/weather/current", async (req, res) => {
    const lat = Number(req.query?.lat);
    const lng = Number(req.query?.lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
      return res.json({ ok: false, error: "lat_lng_required" });
    }
    if (Math.abs(lat) > 90 || Math.abs(lng) > 180) {
      return res.json({ ok: false, error: "lat_lng_invalid" });
    }
    const cacheKey = getWeatherCacheKey(lat, lng);
    const now = Date.now();
    if (weatherUnavailableUntil > now) {
      const waitSec = Math.max(1, Math.ceil((weatherUnavailableUntil - now) / 1000));
      return res.json({ ok: false, error: "weather_upstream_unavailable", retryAfterSec: waitSec });
    }
    const cached = weatherCache.get(cacheKey);
    if (cached && now - Number(cached.ts || 0) < WEATHER_CACHE_TTL_MS) {
      return res.json({ ok: true, provider: "open-meteo", cached: true, ...cached.payload });
    }

    const weatherUrl = `https://api.open-meteo.com/v1/forecast?latitude=${encodeURIComponent(lat)}&longitude=${encodeURIComponent(lng)}&current=temperature_2m,weather_code&timezone=Asia%2FShanghai`;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), WEATHER_FETCH_TIMEOUT_MS);
    try {
      const resp = await fetch(weatherUrl, {
        method: "GET",
        headers: { Accept: "application/json" },
        signal: controller.signal
      });
      if (!resp.ok) {
        return res.json({ ok: false, error: `open_meteo_http_${resp.status}` });
      }
      const data = await resp.json();
      const temperature = Number(data?.current?.temperature_2m);
      const weatherCode = Number(data?.current?.weather_code);
      const payload = {
        temperature: Number.isFinite(temperature) ? temperature : null,
        weatherCode: Number.isFinite(weatherCode) ? weatherCode : null
      };
      weatherUnavailableUntil = 0;
      weatherCache.set(cacheKey, { ts: Date.now(), payload });
      return res.json({ ok: true, provider: "open-meteo", ...payload });
    } catch (err) {
      const msg = err && err.name === "AbortError" ? "weather_timeout" : String(err?.message || err);
      weatherUnavailableUntil = Date.now() + WEATHER_OUTAGE_COOLDOWN_MS;
      return res.json({ ok: false, error: msg });
    } finally {
      clearTimeout(timeoutId);
    }
  });

  app.get("/api/geocode", async (req, res) => {
    const q = String(req.query?.q || "").trim();
    const limit = Math.max(1, Math.min(10, Number(req.query?.limit || 1)));
    if (!q) return res.status(400).json({ ok: false, error: "q_required" });
    const provider = getGeocodeProvider();
    const cacheKey = getGeocodeCacheKey(q, limit, provider);
    const now = Date.now();
    const cached = geocodeCache.get(cacheKey);
    if (cached && now - Number(cached.ts || 0) < GEOCODE_CACHE_TTL_MS) {
      return res.json({ ok: true, query: q, provider, results: cached.results, cached: true });
    }
    if (geocodeRateLimitedUntil > now) {
      const waitSec = Math.max(1, Math.ceil((geocodeRateLimitedUntil - now) / 1000));
      return res.status(429).json({ ok: false, error: "geocode_rate_limited", provider, retryAfterSec: waitSec });
    }
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 10000);
    try {
      if (provider === "amap") {
        const amapUrl = `https://restapi.amap.com/v3/assistant/inputtips?key=${encodeURIComponent(AMAP_WEB_KEY)}&keywords=${encodeURIComponent(q)}&datatype=all&citylimit=false`;
        const amapResp = await fetch(amapUrl, {
          method: "GET",
          headers: { Accept: "application/json" },
          signal: controller.signal
        });
        if (!amapResp.ok) {
          return res.status(amapResp.status).json({ ok: false, error: `amap_http_${amapResp.status}`, provider });
        }
        const amapData = await amapResp.json();
        if (String(amapData?.status) !== "1") {
          const infoCode = String(amapData?.infocode || "");
          const info = String(amapData?.info || "");
          const hardLimitCodes = new Set(["10003", "10004", "10014", "10020", "10021", "10044", "10045"]);
          if (hardLimitCodes.has(infoCode)) {
            geocodeRateLimitedUntil = Date.now() + 60 * 1000;
            return res.status(429).json({ ok: false, error: "geocode_rate_limited", provider, retryAfterSec: 60, detail: info || infoCode });
          }
          return res.status(500).json({
            ok: false,
            error: infoCode ? `amap_${infoCode}` : "amap_failed",
            provider,
            detail: info || ""
          });
        }
        const results = normalizeAmapTips(amapData?.tips, q, limit);
        geocodeRateLimitedUntil = 0;
        geocodeCache.set(cacheKey, { ts: Date.now(), results });
        return res.json({ ok: true, query: q, provider, results });
      }

      const nominatimUrl = `https://nominatim.openstreetmap.org/search?format=json&addressdetails=0&limit=${limit}&q=${encodeURIComponent(q)}`;
      const resp = await fetch(nominatimUrl, {
        method: "GET",
        headers: {
          Accept: "application/json",
          "User-Agent": "TripsServiceHub/1.0 (+local)"
        },
        signal: controller.signal
      });
      if (!resp.ok) {
        if (resp.status === 429) {
          geocodeRateLimitedUntil = Date.now() + 60 * 1000;
          return res.status(429).json({ ok: false, error: "geocode_rate_limited", provider, retryAfterSec: 60 });
        }
        return res.status(resp.status).json({ ok: false, error: `nominatim_http_${resp.status}`, provider });
      }
      const data = await resp.json();
      const rows = Array.isArray(data) ? data : [];
      const results = rows
        .map((row) => ({
          lat: Number(row?.lat),
          lng: Number(row?.lon),
          displayName: String(row?.display_name || row?.name || "").trim()
        }))
        .filter((x) => Number.isFinite(x.lat) && Number.isFinite(x.lng));
      geocodeRateLimitedUntil = 0;
      geocodeCache.set(cacheKey, { ts: Date.now(), results });
      return res.json({ ok: true, query: q, provider, results });
    } catch (err) {
      const msg = err && err.name === "AbortError" ? "geocode_timeout" : String(err?.message || err);
      return res.status(500).json({ ok: false, error: msg, provider });
    } finally {
      clearTimeout(timeoutId);
    }
  });

  app.listen(HUB_PORT, () => {
    process.stdout.write(`service-hub listening on http://127.0.0.1:${HUB_PORT}\n`);
    process.stdout.write(`service-hub workdir: ${path.resolve(WORKDIR)}\n`);
  });
}

main().catch((err) => {
  process.stderr.write(`service-hub failed: ${String(err?.message || err)}\n`);
  process.exit(1);
});
