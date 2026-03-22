const express = require("express");
const path = require("path");
const fs = require("fs/promises");
const fsSync = require("fs");
const { execFile } = require("child_process");
const sharp = require("sharp");
const { chromium } = require("playwright");
const { createWorker } = require("tesseract.js");

const argvPort = process.argv.find((arg) => typeof arg === "string" && arg.startsWith("--port="));
const PORT = Number(argvPort ? argvPort.split("=")[1] : (process.env.PRICE_AGENT_PORT || 7788));
const MAX_ITEMS = 12;
const NAV_TIMEOUT_MS = 15000;
const DEFAULT_MANUAL_CHALLENGE_WAIT_MS = 45000;
const VISIBLE_WAIT_MS = 7000;
const CDP_URL = process.env.PRICE_AGENT_CDP_URL || "http://127.0.0.1:9222";
const FORCE_LOCAL_PROFILE = process.env.PRICE_AGENT_FORCE_LOCAL_PROFILE === "1";
const REQUIRE_CDP = process.env.PRICE_AGENT_REQUIRE_CDP === "1";
const argvSource = process.argv.find((arg) => typeof arg === "string" && arg.startsWith("--source="));
const SOURCE = (argvSource ? argvSource.split("=")[1] : (process.env.PRICE_AGENT_SOURCE || "browser")).toLowerCase();

function resolveAdbPath() {
  if (process.env.PRICE_AGENT_ADB_PATH) return process.env.PRICE_AGENT_ADB_PATH;
  const candidates = [
    path.resolve(process.cwd(), "scrcpy-win64-v3.3.4", "adb.exe"),
    "D:\\JointProjects\\Trips\\scrcpy-win64-v3.3.4\\adb.exe",
    "D:\\Projects\\Trips\\scrcpy-win64-v3.3.4\\adb.exe"
  ];
  for (const p of candidates) {
    try {
      if (fsSync.existsSync(p)) return p;
    } catch (_err) {
      // ignore
    }
  }
  return "adb";
}

const ADB_PATH = resolveAdbPath();
const ADB_DEVICE = process.env.PRICE_AGENT_ADB_DEVICE || "";
const MOBILE_PAGE_WAIT_MS = Number(process.env.PRICE_AGENT_MOBILE_WAIT_MS || 8000);
const MOBILE_TMP_DIR = path.resolve(process.cwd(), ".price-agent-mobile");
const MOBILE_BROWSER_PACKAGE = process.env.PRICE_AGENT_MOBILE_BROWSER_PACKAGE || "com.huawei.browser";
const MOBILE_STEP_WAIT_MS = Number(process.env.PRICE_AGENT_MOBILE_STEP_WAIT_MS || 1200);
const MOBILE_APP_FALLBACK_TO_BROWSER = process.env.PRICE_AGENT_APP_FALLBACK_TO_BROWSER === "1";
const WORKFLOW_FILE = path.join(MOBILE_TMP_DIR, "vlm-workflows.json");
const PRICE_HISTORY_FILE = path.join(MOBILE_TMP_DIR, "travel-price-history.json");
const PRICE_SHOT_FILE = path.join(MOBILE_TMP_DIR, "travel-price-shots.json");
const OCR_PREVIEW_TTL_MS = 5 * 60 * 1000;
const OCR_PREVIEW_CACHE = new Map();

let ocrWorkerPromise = null;
let ACTIVE_WORKFLOW_RUN = null;

function getActiveWorkflowSignal() {
  return ACTIVE_WORKFLOW_RUN?.controller?.signal || null;
}

function isAbortError(err) {
  if (!err) return false;
  const name = String(err.name || "");
  const msg = String(err.message || err);
  return name === "AbortError" || /aborted|abort/i.test(msg);
}

function buildWorkflowAbortError() {
  const e = new Error("workflow_aborted");
  e.name = "WorkflowAbortError";
  return e;
}

function stopActiveWorkflow(reason = "manual_stop") {
  const run = ACTIVE_WORKFLOW_RUN;
  if (!run || !run.controller) return false;
  run.stopReason = reason;
  if (!run.controller.signal.aborted) {
    try {
      run.controller.abort();
    } catch (_err) {
      // ignore
    }
  }
  return true;
}

function normalizeText(text) {
  return (text || "").replace(/\s+/g, " ").trim();
}

function decodeSafe(v) {
  try {
    return decodeURIComponent(v || "");
  } catch (_err) {
    return String(v || "");
  }
}

function parseTripFromUrl(url) {
  const raw = String(url || "");
  const out = {
    fromCityName: "北京",
    toCityName: "",
    fromCode: "BJS",
    toCode: "",
    depDate: "",
    retDate: ""
  };
  try {
    const u = new URL(raw);
    const p = u.searchParams;
    out.fromCityName = decodeSafe(p.get("from") || p.get("fromCity") || out.fromCityName);
    out.toCityName = decodeSafe(p.get("to") || p.get("toCity") || out.toCityName);
    out.fromCode = (p.get("fromCode") || out.fromCode).toUpperCase();
    out.toCode = (p.get("toCode") || out.toCode).toUpperCase();
    out.depDate = p.get("fromDate") || p.get("checkin") || out.depDate;
    out.retDate = p.get("toDate") || p.get("checkout") || out.retDate;
    const ctripPair = p.get("depdate");
    if (ctripPair && ctripPair.includes("_")) {
      const [d1, d2] = ctripPair.split("_");
      out.depDate = out.depDate || d1;
      out.retDate = out.retDate || d2;
    } else if (ctripPair && !out.depDate) {
      out.depDate = ctripPair;
    }
    if (!out.depDate || !out.retDate) {
      const datePair = p.get("date");
      if (datePair && datePair.includes(",")) {
        const [d1, d2] = datePair.split(",");
        out.depDate = out.depDate || d1;
        out.retDate = out.retDate || d2;
      }
    }
    if ((!out.fromCode || !out.toCode) && /\/([A-Z]{3})-([A-Z]{3})/i.test(u.pathname)) {
      const m = u.pathname.match(/\/([A-Z]{3})-([A-Z]{3})/i);
      out.fromCode = out.fromCode || String(m[1]).toUpperCase();
      out.toCode = out.toCode || String(m[2]).toUpperCase();
    }
  } catch (_err) {
    // no-op
  }
  return out;
}

function sleep(ms, signal = getActiveWorkflowSignal()) {
  const wait = Math.max(0, Number(ms) || 0);
  if (!wait) return Promise.resolve();
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(buildWorkflowAbortError());
      return;
    }
    const timer = setTimeout(() => {
      if (signal && onAbort) signal.removeEventListener("abort", onAbort);
      resolve();
    }, wait);
    const onAbort = signal ? () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", onAbort);
      reject(buildWorkflowAbortError());
    } : null;
    if (signal && onAbort) signal.addEventListener("abort", onAbort, { once: true });
  });
}

async function readJsonFileSafe(filePath, fallback) {
  try {
    const raw = await fs.readFile(filePath, "utf8");
    return JSON.parse(raw);
  } catch (_err) {
    return fallback;
  }
}

async function writeJsonFileSafe(filePath, data) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.writeFile(filePath, JSON.stringify(data, null, 2), "utf8");
}

function parseNum(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function parseCoordPair(txt) {
  const m = String(txt || "").match(/^\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*$/);
  if (!m) return null;
  return { x: Number(m[1]), y: Number(m[2]) };
}

function resolveTemplateText(raw, vars = {}) {
  const input = String(raw == null ? "" : raw);
  return input.replace(/\{([^}]+)\}/g, (_m, key) => {
    const k = String(key || "").trim();
    if (!k) return "";
    const v = vars[k];
    if (v == null) return `{${k}}`;
    return String(v);
  });
}

function normalizePlatformCode(raw) {
  const s = String(raw || "").trim();
  if (!s) return "";
  const l = s.toLowerCase();
  if (/^(skyscanner|tianxun)$/.test(l) || /天巡/.test(s)) return "skyscanner";
  if (/^(ctrip)$/.test(l) || /携程/.test(s)) return "ctrip";
  if (/^(qunar)$/.test(l) || /去哪儿|去哪兒/.test(s)) return "qunar";
  if (/^(tongcheng|ly)$/.test(l) || /同程/.test(s)) return "tongcheng";
  return "";
}

function platformCodeToLabel(code) {
  const c = String(code || "").toLowerCase();
  if (c === "skyscanner") return "天巡";
  if (c === "ctrip") return "携程";
  if (c === "qunar") return "去哪儿";
  if (c === "tongcheng") return "同程";
  return "未知平台";
}

function normalizeTimeToken(raw) {
  const txt = String(raw || "").trim().replace("：", ":");
  const m = txt.match(/^([01]?\d|2[0-3]):([0-5]\d)$/);
  if (!m) return "";
  const hh = String(m[1]).padStart(2, "0");
  const mm = String(m[2]).padStart(2, "0");
  return `${hh}:${mm}`;
}

function extractTimeTokens(text) {
  const out = [];
  const reg = /([01]?\d|2[0-3])[:：]([0-5]\d)/g;
  let m;
  while ((m = reg.exec(String(text || "")))) {
    const token = normalizeTimeToken(`${m[1]}:${m[2]}`);
    if (token) out.push(token);
  }
  return out;
}

function extractPriceCandidates(text, opts = {}) {
  const requireCurrency = !!opts.requireCurrency;
  const strictRange = opts.strictRange !== false;
  const minPrice = strictRange ? 300 : 100;
  const maxPrice = strictRange ? 5000 : 9999;
  const out = [];
  const raw = String(text || "");
  const currencyReg = /[¥￥]\s*([0-9][0-9,]{2,6})/g;
  let m;
  while ((m = currencyReg.exec(raw))) {
    const n = Number(String(m[1]).replace(/,/g, ""));
    if (Number.isFinite(n) && n >= minPrice && n <= maxPrice) out.push(n);
  }
  if (out.length) return out;
  if (requireCurrency) return out;
  // Fallback: only allow 3-4 digit numbers to avoid OCR concatenation noise
  const numReg = /\b([1-9]\d{2,3})\b/g;
  while ((m = numReg.exec(raw))) {
    const n = Number(m[1]);
    if (Number.isFinite(n) && n >= minPrice && n <= maxPrice) out.push(n);
  }
  return out;
}

function parseHmToMinutes(hm) {
  const m = String(hm || "").match(/^(\d{2}):(\d{2})$/);
  if (!m) return null;
  return Number(m[1]) * 60 + Number(m[2]);
}

function isPlausibleDomesticDuration(depTime, arrTime) {
  const dep = parseHmToMinutes(depTime);
  const arr = parseHmToMinutes(arrTime);
  if (!Number.isFinite(dep) || !Number.isFinite(arr)) return false;
  let diff = arr - dep;
  if (diff < 0) diff += 24 * 60;
  // Domestic flights should typically be within a practical range.
  return diff >= 60 && diff <= 480;
}

function extractAirlineName(text) {
  const raw = String(text || "");
  const cnMatch = raw.match(/([\u4e00-\u9fa5]{2,8}航空)/);
  if (cnMatch) return cnMatch[1];
  const enMatch = raw.match(/([A-Z][A-Za-z ]{2,20}(?:Air|Airlines|Airways))/);
  if (enMatch) return enMatch[1].trim();
  return "";
}

function detectPlatformFromText(url, title = "", text = "") {
  const blob = `${url || ""} ${title || ""} ${text || ""}`;
  if (/ctrip|携程|trip\.com/i.test(blob)) return "ctrip";
  if (/qunar|去哪儿|去哪兒/i.test(blob)) return "qunar";
  if (/skyscanner|天巡/i.test(blob)) return "skyscanner";
  if (/tongcheng|同程|ly\.com|艺龙|藝龍/i.test(blob)) return "tongcheng";
  return "";
}

function detectPlatformFromUrl(url) {
  const raw = String(url || "");
  try {
    const u = new URL(raw);
    const hostCode = normalizePlatformCode(u.hostname || "");
    if (hostCode) return hostCode;
  } catch (_err) {
    // ignore
  }
  return normalizePlatformCode(raw);
}

async function fetchCdpTargets() {
  try {
    const resp = await fetch(`${CDP_URL}/json`);
    if (!resp.ok) return [];
    const data = await resp.json();
    if (!Array.isArray(data)) return [];
    return data.filter((x) => x && x.type === "page");
  } catch (_err) {
    return [];
  }
}

async function cdpCaptureScreenshot(wsUrl, timeoutMs = 12000) {
  return new Promise((resolve, reject) => {
    let timer;
    let idCounter = 1;
    const ws = new WebSocket(wsUrl);
    const pending = new Map();

    const cleanup = (err) => {
      if (timer) clearTimeout(timer);
      try { ws.close(); } catch (_err) {}
      if (err) reject(err);
    };

    const send = (method, params = {}) => {
      const id = idCounter++;
      pending.set(id, { method });
      ws.send(JSON.stringify({ id, method, params }));
      return id;
    };

    ws.onopen = () => {
      send("Page.enable");
      send("Runtime.enable");
      send("Page.captureScreenshot", { format: "png", fromSurface: true });
    };
    ws.onerror = (evt) => cleanup(new Error(`cdp_ws_error:${evt?.message || "unknown"}`));
    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(String(evt.data || "{}"));
        if (!msg.id) return;
        const pendingReq = pending.get(msg.id);
        pending.delete(msg.id);
        if (pendingReq?.method === "Page.captureScreenshot") {
          const data = msg?.result?.data;
          if (!data) return cleanup(new Error("cdp_screenshot_empty"));
          const buf = Buffer.from(String(data), "base64");
          cleanup();
          resolve({ buffer: buf });
        }
      } catch (err) {
        cleanup(err);
      }
    };

    timer = setTimeout(() => cleanup(new Error("cdp_timeout")), timeoutMs);
  });
}

async function cdpEvaluate(wsUrl, expression, timeoutMs = 12000) {
  return new Promise((resolve, reject) => {
    let timer;
    let idCounter = 1;
    const ws = new WebSocket(wsUrl);
    const pending = new Map();

    const cleanup = (err) => {
      if (timer) clearTimeout(timer);
      try { ws.close(); } catch (_err) {}
      if (err) reject(err);
    };

    const send = (method, params = {}) => {
      const id = idCounter++;
      pending.set(id, { method });
      ws.send(JSON.stringify({ id, method, params }));
      return id;
    };

    ws.onopen = () => {
      send("Runtime.enable");
      send("Runtime.evaluate", {
        expression,
        returnByValue: true,
        awaitPromise: true
      });
    };
    ws.onerror = (evt) => cleanup(new Error(`cdp_ws_error:${evt?.message || "unknown"}`));
    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(String(evt.data || "{}"));
        if (!msg.id) return;
        const req = pending.get(msg.id);
        pending.delete(msg.id);
        if (req?.method === "Runtime.evaluate") {
          if (msg?.exceptionDetails) return cleanup(new Error("cdp_eval_exception"));
          cleanup();
          resolve(msg?.result?.result?.value);
        }
      } catch (err) {
        cleanup(err);
      }
    };
    timer = setTimeout(() => cleanup(new Error("cdp_eval_timeout")), timeoutMs);
  });
}

function parseFlightItemsFromText(text) {
  const lines = String(text || "")
    .split(/\r?\n+/)
    .map((l) => normalizeText(l))
    .filter(Boolean);
  const seen = new Set();
  const items = [];

  const readLine = (line) => {
    const times = extractTimeTokens(line);
    let prices = extractPriceCandidates(line, { requireCurrency: true });
    // OCR often drops the currency symbol; allow numeric fallback on same line.
    if (!prices.length) {
      prices = extractPriceCandidates(line, { requireCurrency: false });
    }
    if (times.length < 2 || !prices.length) return;
    const depTime = times[0];
    const arrTime = times[1];
    if (!isPlausibleDomesticDuration(depTime, arrTime)) return;
    const amount = prices[0];
    const airline = extractAirlineName(line);
    const key = `${depTime}_${arrTime}_${amount}`;
    if (seen.has(key)) return;
    seen.add(key);
    items.push({ depTime, arrTime, amount, airline });
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    readLine(line);
    if (i + 1 < lines.length) {
      readLine(`${line} ${lines[i + 1]}`);
    }
  }
  const dedupByTime = new Map();
  items.forEach((it) => {
    const k = `${it.depTime}_${it.arrTime}`;
    const prev = dedupByTime.get(k);
    if (!prev || Number(it.amount || 0) < Number(prev.amount || 0)) {
      dedupByTime.set(k, it);
    }
  });
  return [...dedupByTime.values()]
    .filter((x) => Number.isFinite(Number(x.amount)) && x.amount >= 300 && x.amount <= 5000)
    .sort((a, b) => {
      const ta = parseHmToMinutes(a.depTime) || 0;
      const tb = parseHmToMinutes(b.depTime) || 0;
      if (ta !== tb) return ta - tb;
      return Number(a.amount || 0) - Number(b.amount || 0);
    });
}

function parseFlightItemsFromDomText(text) {
  const lines = String(text || "")
    .split(/\r?\n+/)
    .map((l) => normalizeText(l))
    .filter(Boolean);
  const out = [];
  const seen = new Set();
  const isTime = (s) => /^\d{1,2}:\d{2}$/.test(String(s || ""));
  const getPrice = (s) => {
    const m = String(s || "").match(/[¥￥]\s*([1-9]\d{2,3})/);
    if (!m) return null;
    const n = Number(m[1]);
    if (!Number.isFinite(n) || n < 300 || n > 5000) return null;
    return n;
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const timesInLine = extractTimeTokens(line).map((t) => normalizeTimeToken(t)).filter(Boolean);
    // Case 1: dep/arr appear in the same rendered line.
    if (timesInLine.length >= 2) {
      const t1 = timesInLine[0];
      const t2 = timesInLine[1];
      if (isTime(t1) && isTime(t2) && isPlausibleDomesticDuration(t1, t2)) {
        let price = getPrice(line);
        if (price == null && i + 1 < lines.length) price = getPrice(lines[i + 1]);
        if (price == null && i + 2 < lines.length) price = getPrice(lines[i + 2]);
        if (price != null) {
          const key = `${t1}_${t2}`;
          if (!seen.has(key)) {
            seen.add(key);
            out.push({ depTime: t1, arrTime: t2, amount: price, airline: "" });
            continue;
          }
        }
      }
    }

    // Case 2: dep/arr are split into nearby lines.
    const t1 = normalizeTimeToken(line);
    if (!isTime(t1)) continue;
    for (let j = i + 1; j <= Math.min(i + 4, lines.length - 1); j += 1) {
      const t2 = normalizeTimeToken(lines[j]);
      if (!isTime(t2)) continue;
      if (!isPlausibleDomesticDuration(t1, t2)) continue;
      let price = null;
      for (let k = j; k <= Math.min(j + 8, lines.length - 1); k += 1) {
        price = getPrice(lines[k]);
        if (price != null) break;
      }
      if (price == null) continue;
      const key = `${t1}_${t2}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ depTime: t1, arrTime: t2, amount: price, airline: "" });
      break;
    }
  }

  return out.sort((a, b) => {
    const ta = parseHmToMinutes(a.depTime) || 0;
    const tb = parseHmToMinutes(b.depTime) || 0;
    return ta - tb;
  });
}

function normalizeExtractedFlightItems(items) {
  const list = Array.isArray(items) ? items : [];
  const out = [];
  const seen = new Set();
  list.forEach((raw) => {
    const depTime = normalizeTimeToken(raw?.depTime || "");
    const arrTime = normalizeTimeToken(raw?.arrTime || "");
    const amount = Number(raw?.amount);
    if (!depTime || !arrTime || !Number.isFinite(amount)) return;
    if (amount < 300 || amount > 5000) return;
    if (!isPlausibleDomesticDuration(depTime, arrTime)) return;
    const key = `${depTime}_${arrTime}`;
    const row = {
      depTime,
      arrTime,
      amount,
      airline: String(raw?.airline || "").trim(),
      flightNo: String(raw?.flightNo || "").trim(),
      depAirport: String(raw?.depAirport || "").trim(),
      arrAirport: String(raw?.arrAirport || "").trim()
    };
    // Keep all visible flights; only remove exact duplicates.
    const uniqKey = `${key}_${row.amount}_${row.airline}_${row.flightNo}_${row.depAirport}_${row.arrAirport}`;
    if (seen.has(uniqKey)) return;
    seen.add(uniqKey);
    out.push(row);
  });
  return out.sort((a, b) => {
    const ta = parseHmToMinutes(a.depTime) || 0;
    const tb = parseHmToMinutes(b.depTime) || 0;
    if (ta !== tb) return ta - tb;
    return Number(a.amount || 0) - Number(b.amount || 0);
  });
}

function buildFlightCodeExtractExpression(platformCode = "") {
  const hint = String(platformCode || "").toLowerCase();
  return `(() => {
    const platformHint = ${JSON.stringify(hint)};
    const clean = (s) => String(s || "").replace(/\\s+/g, " ").trim();
    const normalizeTime = (s) => {
      const m = clean(s).match(/([01]?\\d|2[0-3])[:：]([0-5]\\d)/);
      if (!m) return "";
      return String(m[1]).padStart(2, "0") + ":" + String(m[2]).padStart(2, "0");
    };
    const extractTimes = (s) => {
      const out = [];
      const reg = /([01]?\\d|2[0-3])[:：]([0-5]\\d)/g;
      const txt = String(s || "");
      let m;
      while ((m = reg.exec(txt))) {
        out.push(String(m[1]).padStart(2, "0") + ":" + String(m[2]).padStart(2, "0"));
      }
      return out;
    };
    const parsePrice = (s) => {
      const txt = clean(s);
      if (!txt) return null;
      const num = txt.replace(/[^0-9]/g, "");
      if (!num) return null;
      const n = Number(num);
      if (!Number.isFinite(n)) return null;
      if (n >= 300 && n <= 5000) return n;
      return null;
    };
    const toItem = (row) => {
      const rowText = clean(row?.innerText || row?.textContent || "");
      if (!rowText) return null;
      if (rowText.length > 700) return null;
      const depNode = row.querySelector(".sep-lf, .from .time, .depart .time, .depart-time, .time-depart, [data-role='dep-time']");
      const arrNode = row.querySelector(".sep-rt, .to .time, .arrive .time, .arrive-time, .time-arrive, [data-role='arr-time']");
      let depTime = normalizeTime(depNode?.textContent || "");
      let arrTime = normalizeTime(arrNode?.textContent || "");
      if (!depTime || !arrTime) {
        const times = extractTimes(rowText);
        if (!depTime) depTime = times[0] || "";
        if (!arrTime) arrTime = times[1] || "";
      }
      if (!depTime || !arrTime) return null;
      const airline = clean(
        row.querySelector(".air, .airline-name, .company-name, [data-role='airline']")?.textContent
        || row.querySelector(".company, .airline")?.textContent
        || ""
      );
      const flightNo = clean(
        row.querySelector(".num .n, .flightNo, .flight-no, [data-role='flight-no']")?.textContent
        || row.querySelector(".flight_no, .air-no")?.textContent
        || ""
      );
      const airportNodes = Array.from(row.querySelectorAll(".airport, .airport-name, .station, .terminal"));
      const depAirport = clean(airportNodes[0]?.textContent || "");
      const arrAirport = clean(airportNodes[1]?.textContent || "");
      const extractRowPrice = (root, txt) => {
        const selectors = [
          ".col-price .prc .fix_price",
          ".fix_price",
          ".price .num",
          ".ticket-price",
          ".col-price .prc",
          "[data-role='price']",
          "[class*='price'][title]",
          "[class*='price']",
          "[class*='prc']"
        ];
        const nodes = [];
        const seen = new Set();
        for (const sel of selectors) {
          const list = Array.from(root.querySelectorAll(sel));
          for (const n of list) {
            if (!n || seen.has(n)) continue;
            seen.add(n);
            nodes.push(n);
          }
        }
        const prices = [];
        for (const n of nodes) {
          const p1 = parsePrice(n?.getAttribute?.("title") || "");
          const p2 = parsePrice(n?.textContent || "");
          if (p1) prices.push(p1);
          if (p2) prices.push(p2);
        }
        const fallbackToken = (txt.match(/[¥￥]\\s*[0-9][0-9,]*/) || [])[0] || "";
        const fallbackRise = (txt.match(/([3-9]\\d{2}|[1-4]\\d{3})\\s*起/) || [])[1] || "";
        const p3 = parsePrice(fallbackToken);
        const p4 = parsePrice(fallbackRise);
        if (p3) prices.push(p3);
        if (p4) prices.push(p4);
        if (!prices.length) return null;
        return Math.min(...prices);
      };
      const amount = extractRowPrice(row, rowText);
      if (!amount) return null;
      return { depTime, arrTime, amount, airline, flightNo, depAirport, arrAirport };
    };

    const rowSelectors = [
      ".b-airfly",
      ".flight-item",
      ".flight-list-item",
      ".result-item",
      ".ticket-item",
      "li[class*='flight']",
      "div[class*='flight-item']",
      "div[class*='flt-item']",
      "li[class*='flt-item']",
      "[data-testid*='flight']",
      "[class*='flightRow']",
      "[class*='flight-row']"
    ];
    const rows = [];
    const rowSeen = new Set();
    for (const sel of rowSelectors) {
      const list = Array.from(document.querySelectorAll(sel));
      for (const r of list) {
        if (!r || rowSeen.has(r)) continue;
        rowSeen.add(r);
        rows.push(r);
      }
    }
    const items = rows.map(toItem).filter(Boolean);
    const platform = platformHint || (/qunar|去哪儿/i.test(location.host + " " + document.title) ? "qunar" : "");
    return {
      platform,
      rowCount: rows.length,
      itemCount: items.length,
      items
    };
  })()`;
}

function cleanHotelName(title) {
  let name = String(title || "").trim();
  name = name.replace(/-?\s*(携程|去哪儿|同程|天巡|酒店预订|酒店预定|酒店|trip\.com).*$/i, "").trim();
  return name;
}

function parseHotelInfoFromText(text, title, nights = []) {
  const prices = extractPriceCandidates(text);
  const amount = prices.length ? Math.min(...prices) : null;
  const rawDates = extractDatesFromText(text);
  let checkinDate = "";
  if (rawDates.length) {
    const first = rawDates[0];
    if (first) {
      const y = first.y || new Date().getFullYear();
      const m = String(first.m || "").padStart(2, "0");
      const d = String(first.d || "").padStart(2, "0");
      checkinDate = `${y}-${m}-${d}`;
    }
  }
  if (!checkinDate && Array.isArray(nights) && nights.length) {
    checkinDate = String(nights[0]?.date || "");
  }
  const nightMatch = Array.isArray(nights)
    ? nights.find((n) => String(n?.date || "") === checkinDate)
    : null;
  const hotelName = String(nightMatch?.hotelName || "").trim() || cleanHotelName(title) || "未知酒店";
  return { amount, checkinDate, hotelName };
}

function putOcrPreview(payload) {
  const previewId = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
  OCR_PREVIEW_CACHE.set(previewId, { ...payload, createdAt: Date.now() });
  return previewId;
}

function getOcrPreview(previewId) {
  const data = OCR_PREVIEW_CACHE.get(previewId);
  if (!data) return null;
  if (Date.now() - Number(data.createdAt || 0) > OCR_PREVIEW_TTL_MS) {
    OCR_PREVIEW_CACHE.delete(previewId);
    return null;
  }
  return data;
}

function resolvePlatformFromEntry(entry = {}) {
  const direct = normalizePlatformCode(entry.platform || entry.platformCode);
  if (direct) return direct;
  const fromWf = normalizePlatformCode(entry.workflowId || entry.workflowName);
  if (fromWf) return fromWf;
  return "";
}

function normalizeHistoryRecord(record) {
  if (!record || typeof record !== "object") return null;
  const code = resolvePlatformFromEntry(record);
  const amount = Number(record.amount);
  if (!code) return null;
  if (!Number.isFinite(amount) || amount <= 0) return null;
  const tripType = String(record.tripType || "round").trim() || "round";
  const direction = String(record.direction || "").trim() || (tripType === "oneway" ? "outbound" : "round");
  const depTime = String(record.depTime || "").trim();
  const arrTime = String(record.arrTime || "").trim();
  const flightNo = String(record.flightNo || "").trim().toUpperCase();
  return {
    ...record,
    platform: code,
    platformCode: code,
    platformLabel: platformCodeToLabel(code),
    amount,
    tripType,
    direction,
    depTime,
    arrTime,
    flightNo
  };
}

function buildPriceShotKey(planId, category, platformCode, refKey = "") {
  return `${planId}__${category || "flight"}__${platformCode || ""}__${refKey || ""}`;
}

function normalizePriceShotRecord(record) {
  if (!record || typeof record !== "object") return null;
  const planId = String(record.planId || "").trim();
  const code = resolvePlatformFromEntry(record);
  const imageBase64 = String(record.image_base64 || "").trim();
  const width = Number(record.width || 0);
  const height = Number(record.height || 0);
  if (!planId || !code || !imageBase64) return null;
  const category = String(record.category || "flight").trim() || "flight";
  const refKey = String(record.refKey || "");
  const displayPlatform = String(record.displayPlatform || record.platformLabel || platformCodeToLabel(code) || "");
  const key = String(record.key || "").trim() || buildPriceShotKey(planId, category, code, refKey);
  return {
    planId,
    key,
    category,
    refKey,
    displayPlatform,
    platform: code,
    platformCode: code,
    platformLabel: platformCodeToLabel(code),
    image_base64: imageBase64,
    width: Number.isFinite(width) ? width : 0,
    height: Number.isFinite(height) ? height : 0,
    planName: String(record.planName || ""),
    depDate: String(record.depDate || ""),
    retDate: String(record.retDate || ""),
    updatedAt: Number(record.updatedAt || Date.now())
  };
}

async function savePriceShot(shot) {
  const normalized = normalizePriceShotRecord(shot);
  if (!normalized) throw new Error("invalid_price_shot");
  const payload = await readJsonFileSafe(PRICE_SHOT_FILE, { shots: {} });
  const map = payload && typeof payload.shots === "object" && payload.shots ? payload.shots : {};
  map[normalized.key] = normalized;
  const next = { shots: map, updatedAt: Date.now() };
  await writeJsonFileSafe(PRICE_SHOT_FILE, next);
  return next;
}

async function listPriceShots(planId = "") {
  const payload = await readJsonFileSafe(PRICE_SHOT_FILE, { shots: {} });
  const map = payload && typeof payload.shots === "object" && payload.shots ? payload.shots : {};
  const entries = Object.values(map).map((x) => normalizePriceShotRecord(x)).filter(Boolean);
  const list = planId ? entries.filter((x) => x.planId === planId) : entries;
  return {
    shots: list,
    updatedAt: Number(payload.updatedAt || Date.now())
  };
}

function buildWorkflowVars(context = {}) {
  const depDate = String(context.depDate || "");
  const retDate = String(context.retDate || "");
  const fromCity = String(context.fromCity || context["出发地"] || "北京");
  const toCity = String(context.toCity || context.destination || context["目的地"] || "");
  let toCode = String(context.toCode || context["目的地代码"] || "");
  if (!toCode) {
    const cityCodeMap = {
      "北京": "BJS",
      "福州": "FOC",
      "三亚": "SYX",
      "重庆": "CKG",
      "海口": "HAK",
      "桂林": "KWL",
      "佛山": "FUO",
      "昆明": "KMG",
      "平潭": "FOC",
      "大理": "DLI",
      "大连": "DLC",
      "呼和浩特": "HET",
      "日照": "RIZ",
      "青岛": "TAO"
    };
    toCode = cityCodeMap[toCity] || "";
  }
  return {
    目的地: toCity,
    目的地代码: toCode,
    出发地: fromCity,
    出发日期: depDate,
    返程日期: retDate,
    depDate,
    retDate,
    fromCity,
    toCity,
    toCode
  };
}

function parseYmd(dateStr) {
  const s = String(dateStr || "").trim().replace(/年/g, "-").replace(/月/g, "-").replace(/日/g, "");
  const m = s.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/);
  if (!m) return null;
  return {
    y: Number(m[1]),
    m: Number(m[2]),
    d: Number(m[3])
  };
}

function extractDatesFromText(text) {
  const s = String(text || "");
  const out = [];
  const reg = /(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?/g;
  let m;
  while ((m = reg.exec(s)) !== null) {
    const y = Number(m[1]);
    const mm = Number(m[2]);
    const dd = Number(m[3]);
    if (y >= 2020 && y <= 2100 && mm >= 1 && mm <= 12 && dd >= 1 && dd <= 31) {
      out.push(`${y}-${String(mm).padStart(2, "0")}-${String(dd).padStart(2, "0")}`);
    }
  }
  return out;
}

function firstDayColumnOfMonth(y, m) {
  return new Date(y, m - 1, 1).getDay(); // 0..6, Sunday first
}

function monthMatchScore(text, y, m) {
  const t = String(text || "").replace(/\s+/g, "");
  if (!t) return 0;
  const zhMap = { "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12 };
  const mMatch = t.match(/(1[0-2]|0?[1-9])月/);
  const zhMatch = t.match(/(十一|十二|十|[一二三四五六七八九])月/);
  const monthInText = mMatch ? Number(mMatch[1]) : (zhMatch ? zhMap[zhMatch[1]] : null);
  let score = 0;
  if (t.includes(`${y}年${m}月`)) score += 100;
  if (t.includes(`${y}-${m}`) || t.includes(`${y}/${m}`) || t.includes(`${y}.${m}`)) score += 90;
  if (t.includes(`${y}年`) && t.includes(`${m}月`)) score += 80;
  if (monthInText === m) score += 70; // 支持“3月/三月”（无年份）
  return score;
}

function inCalendarSafeRegion(bounds, screenSize) {
  if (!bounds || !screenSize) return false;
  const cx = Number(bounds.cx);
  const cy = Number(bounds.cy);
  const w = Number(screenSize.width || 0);
  const h = Number(screenSize.height || 0);
  if (!Number.isFinite(cx) || !Number.isFinite(cy) || w <= 0 || h <= 0) return false;
  // Avoid top header/close area and bottom summary+confirm area.
  if (cy < h * 0.20) return false;
  if (cy > h * 0.78) return false;
  // Avoid extreme left/right edges.
  if (cx < w * 0.05 || cx > w * 0.95) return false;
  return true;
}

function hasCalendarForbiddenText(text) {
  const t = String(text || "");
  // 注意：不要把“低价位”当禁词，否则天巡日期格子会被误过滤。
  return /(仅看直飞|选择往返日期|往返总价|搜索往返机票)/.test(t);
}

function dateNodeMatchScore(text, ymd) {
  const t = String(text || "").replace(/\s+/g, "");
  if (!t) return 0;
  const mm = String(ymd.m);
  const dd = String(ymd.d);
  const mm2 = String(ymd.m).padStart(2, "0");
  const dd2 = String(ymd.d).padStart(2, "0");
  let score = 0;
  if (t.includes(`${ymd.y}年${mm}月${dd}日`)) score += 120;
  if (t.includes(`${ymd.y}-${mm2}-${dd2}`) || t.includes(`${ymd.y}/${mm2}/${dd2}`) || t.includes(`${ymd.y}.${mm2}.${dd2}`)) score += 110;
  if (t.includes(`${mm}月${dd}日`)) score += 90;
  if (t.includes(`${mm2}-${dd2}`) || t.includes(`${mm2}/${dd2}`)) score += 70;
  if (new RegExp(`(^|\\D)${dd}(\\D|$)`).test(t)) score += 20;
  return score;
}

function rankCalendarNode(node, ymd) {
  const txt = `${node.text || ""} ${node.contentDesc || ""}`;
  let score = 0;
  let hit = false;
  if (new RegExp(`${ymd.m}\\s*月\\s*${ymd.d}\\s*日`).test(txt)) {
    score += 100;
    hit = true;
  }
  if (new RegExp(`${ymd.y}[-/年\\s]*${ymd.m}[-/月\\s]*${ymd.d}`).test(txt)) {
    score += 90;
    hit = true;
  }
  if (String(node.text || "").trim() === String(ymd.d)) {
    score += 40;
    hit = true;
  }
  if (new RegExp(`(^|\\D)${ymd.d}(\\D|$)`).test(String(node.text || ""))) {
    score += 30;
    hit = true;
  }
  if (!hit) return 0;
  if (node.clickable) score += 8;
  if (node.enabled) score += 4;
  // Prefer visible calendar grid area (usually not status bar / top title).
  if (node.bounds && node.bounds.cy > 250) score += 4;
  return score;
}

function emitCalendarDebug(handler, payload) {
  if (typeof handler !== "function") return;
  try {
    handler({ ts: Date.now(), ...payload });
  } catch (_err) {
    // ignore debug callback errors
  }
}

async function tapCalendarDate(dateStr, opts = {}) {
  const ymd = parseYmd(dateStr);
  if (!ymd) {
    throw new Error(`invalid_date:${dateStr}`);
  }
  const onDebug = typeof opts.onDebug === "function" ? opts.onDebug : null;
  const retries = Math.max(1, parseNum(opts.retries, 3));
  const waitAfter = Math.max(120, parseNum(opts.waitAfter, 700));
  const avoid = opts.avoidBounds || null;
  const screenSize = await getScreenSize();
  let lastNodes = [];
  emitCalendarDebug(onDebug, {
    type: "calendar_start",
    date: `${ymd.y}-${String(ymd.m).padStart(2, "0")}-${String(ymd.d).padStart(2, "0")}`,
    retries,
    screen: screenSize
  });

  for (let i = 0; i < retries; i += 1) {
    const nodes = await dumpUiNodes("calendar");
    lastNodes = nodes;
    const candidates = nodes
      .map((n) => ({ n, score: rankCalendarNode(n, ymd) }))
      .filter((x) => {
        const txt = `${x.n.text || ""}${x.n.contentDesc || ""}`;
        return x.score > 0 && x.n.bounds && inCalendarSafeRegion(x.n.bounds, screenSize) && !hasCalendarForbiddenText(txt);
      });

    if (!candidates.length) {
      emitCalendarDebug(onDebug, { type: "ui_candidates_empty", attempt: i + 1 });
      await sleep(220);
      continue;
    }
    candidates.sort((a, b) => {
      if (b.score !== a.score) return b.score - a.score;
      if (a.n.bounds.cy !== b.n.bounds.cy) return a.n.bounds.cy - b.n.bounds.cy;
      return a.n.bounds.cx - b.n.bounds.cx;
    });

    let target = candidates[0].n;
    if (avoid) {
      const dist = (n) => Math.hypot(n.bounds.cx - avoid.cx, n.bounds.cy - avoid.cy);
      const nonOverlap = candidates.find((c) => dist(c.n) > 18);
      if (nonOverlap) target = nonOverlap.n;
    }

    emitCalendarDebug(onDebug, {
      type: "ui_candidates",
      attempt: i + 1,
      top: candidates.slice(0, 5).map((c) => ({
        score: c.score,
        text: `${c.n.text || ""} ${c.n.contentDesc || ""}`.trim().slice(0, 40),
        bounds: c.n.bounds
      })),
      chosen: target.bounds
    });
    await adbTap(target.bounds.cx, target.bounds.cy);
    await sleep(waitAfter);
    return { ...target.bounds, method: "ui_node" };
  }

  // OCR fallback: some app calendars hide day cells from UI tree.
  const screenPath = await captureCurrentMobileScreen("calendar_ocr");
  try {
    emitCalendarDebug(onDebug, { type: "ocr_fallback_start" });
    const words = await ocrWords(screenPath);
    const day = ymd.d;
    const screenW = screenSize.width;
    const screenH = screenSize.height;
    // 1) Build month header blocks from UI tree (stable in Ctrip calendar).
    const headerBlocks = (lastNodes || [])
      .filter((n) => n.bounds)
      .filter((n) => {
        const b = n.bounds;
        const w = b.x2 - b.x1;
        const h = b.y2 - b.y1;
        return (
          w >= screenW * 0.5 &&
          w <= screenW * 0.96 &&
          h >= 60 &&
          h <= 220 &&
          b.y1 >= Math.round(screenH * 0.16) &&
          b.y2 <= Math.round(screenH * 0.84)
        );
      })
      .map((n) => n.bounds)
      .sort((a, b) => a.y1 - b.y1);
    emitCalendarDebug(onDebug, {
      type: "header_blocks",
      count: headerBlocks.length,
      blocks: headerBlocks.slice(0, 4)
    });

    // 2) Prefer month text from UI tree; fallback to OCR month labels.
    let targetHeader = null;
    const monthTextNodes = (lastNodes || [])
      .map((n) => ({
        n,
        txt: `${n.text || ""}${n.contentDesc || ""}`,
        score: monthMatchScore(`${n.text || ""}${n.contentDesc || ""}`, ymd.y, ymd.m)
      }))
      .filter((x) => x.score > 0 && x.n.bounds)
      .sort((a, b) => b.score - a.score);
    emitCalendarDebug(onDebug, {
      type: "month_text_nodes",
      top: monthTextNodes.slice(0, 6).map((x) => ({
        score: x.score,
        text: String(x.txt || "").slice(0, 36),
        bounds: x.n.bounds
      }))
    });
    if (headerBlocks.length && monthTextNodes.length) {
      const anchor = monthTextNodes[0].n.bounds;
      targetHeader = headerBlocks
        .map((h) => ({ h, d: Math.abs((h.y1 + h.y2) / 2 - anchor.cy) }))
        .sort((a, b) => a.d - b.d)[0]?.h || null;
      emitCalendarDebug(onDebug, {
        type: "header_pick_from_ui",
        anchor: anchor,
        chosen: targetHeader
      });
    }

    const monthLabels = words
      .map((w) => {
        const t = String(w?.text || "").replace(/\s+/g, "");
        const bbox = w?.bbox || {};
        const x0 = Number(bbox.x0);
        const y0 = Number(bbox.y0);
        const x1 = Number(bbox.x1);
        const y1 = Number(bbox.y1);
        if (![x0, y0, x1, y1].every(Number.isFinite)) return null;
        const full = t.match(/(\d{4})\D{0,4}(1[0-2]|0?[1-9])\D{0,2}月?/);
        if (full) {
          const yy = Number(full[1]);
          const mon = Number(full[2]);
          if (yy >= 2020 && yy <= 2100 && mon >= 1 && mon <= 12) {
            return { y: yy, m: mon, cy: (y0 + y1) / 2 };
          }
        }
        const zhMap = { "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12 };
        const mNum = t.match(/(1[0-2]|0?[1-9])月/);
        if (mNum) {
          const mon = Number(mNum[1]);
          return { y: ymd.y, m: mon, cy: (y0 + y1) / 2 };
        }
        const mZh = t.match(/(十一|十二|十|[一二三四五六七八九])月/);
        if (mZh && zhMap[mZh[1]]) {
          return { y: ymd.y, m: zhMap[mZh[1]], cy: (y0 + y1) / 2 };
        }
        return null;
      })
      .filter(Boolean);
    if (!targetHeader && headerBlocks.length && monthLabels.length) {
      const targetLabel = monthLabels.find((m) => m.y === ymd.y && m.m === ymd.m) || null;
      if (targetLabel) {
        targetHeader = headerBlocks
          .map((h) => ({ h, d: Math.abs((h.y1 + h.y2) / 2 - targetLabel.cy) }))
          .sort((a, b) => a.d - b.d)[0]?.h || null;
        emitCalendarDebug(onDebug, {
          type: "header_pick_from_ocr",
          label: targetLabel,
          chosen: targetHeader
        });
      }
    }
    if (!targetHeader && headerBlocks.length) {
      targetHeader = headerBlocks[0];
      emitCalendarDebug(onDebug, { type: "header_fallback_first", chosen: targetHeader });
    }
    if (!targetHeader && monthTextNodes.length) {
      targetHeader = monthTextNodes[0].n.bounds;
      emitCalendarDebug(onDebug, { type: "header_from_month_text", chosen: targetHeader });
    }

    // 3) Grid click from month block + row containers.
    if (targetHeader) {
      const dayRows = (lastNodes || [])
        .filter((n) => n.bounds)
        .filter((n) => {
          const b = n.bounds;
          const w = b.x2 - b.x1;
          const h = b.y2 - b.y1;
          return (
            w >= screenW * 0.8 &&
            h >= 80 &&
            h <= 280 &&
            b.y1 > targetHeader.y2 - 10 &&
            b.y2 < screenH * 0.92
          );
        })
        .map((n) => n.bounds)
        .sort((a, b) => a.y1 - b.y1);

      const idxHeader = headerBlocks.findIndex((h) => h.y1 === targetHeader.y1 && h.y2 === targetHeader.y2);
      const nextHeader = idxHeader >= 0 && idxHeader < headerBlocks.length - 1 ? headerBlocks[idxHeader + 1] : null;
      const gridTop = dayRows.length ? dayRows[0].y1 : Math.round(targetHeader.y2 + Math.max(20, screenH * 0.02));
      const gridBottom = dayRows.length
        ? dayRows[Math.min(5, dayRows.length - 1)].y2
        : Math.round(nextHeader ? nextHeader.y1 - 10 : Math.min(screenH * 0.83, gridTop + screenH * 0.48));
      const left = dayRows.length ? dayRows[0].x1 : Math.round(screenW * 0.05);
      const right = dayRows.length ? dayRows[0].x2 : Math.round(screenW * 0.95);
      const rowCount = 6;
      const cellW = (right - left) / 7;
      const cellH = (gridBottom - gridTop) / rowCount;
      const firstCol = firstDayColumnOfMonth(ymd.y, ymd.m);
      const index = firstCol + (ymd.d - 1);
      const row = Math.floor(index / 7);
      const col = index % 7;
      emitCalendarDebug(onDebug, {
        type: "grid_calc",
        firstCol,
        index,
        row,
        col,
        rowCount,
        grid: { left, right, top: gridTop, bottom: gridBottom },
        dayRows: dayRows.slice(0, 6)
      });
      if (row >= 0 && row < rowCount && col >= 0 && col <= 6 && cellH > 20) {
        const gx = Math.round(left + (col + 0.5) * cellW);
        const gy = Math.round(gridTop + (row + 0.5) * cellH);
        if (inCalendarSafeRegion({ cx: gx, cy: gy }, screenSize) && (!avoid || Math.hypot(gx - avoid.cx, gy - avoid.cy) > 20)) {
          await adbTap(gx, gy);
          await sleep(waitAfter);
          emitCalendarDebug(onDebug, { type: "grid_tap", point: { x: gx, y: gy } });
          return { x1: gx, y1: gy, x2: gx, y2: gy, cx: gx, cy: gy, method: "grid_calc" };
        }
        emitCalendarDebug(onDebug, { type: "grid_tap_rejected", point: { x: gx, y: gy } });
      } else {
        emitCalendarDebug(onDebug, { type: "grid_calc_invalid", row, col, rowCount, cellH });
      }
    }

    // 3b) Month header仍未命中时，按通用日历区域比例兜底一次（适配天巡等无年份月头样式）
    if (!targetHeader) {
      const firstCol = firstDayColumnOfMonth(ymd.y, ymd.m);
      const index = firstCol + (ymd.d - 1);
      const row = Math.floor(index / 7);
      const col = index % 7;
      const left = Math.round(screenW * 0.08);
      const right = Math.round(screenW * 0.92);
      const gridTop = Math.round(screenH * 0.33);
      const gridBottom = Math.round(screenH * 0.74);
      const cellW = (right - left) / 7;
      const cellH = (gridBottom - gridTop) / 6;
      const gx = Math.round(left + (col + 0.5) * cellW);
      const gy = Math.round(gridTop + (row + 0.5) * cellH);
      emitCalendarDebug(onDebug, {
        type: "grid_calc_no_header",
        firstCol,
        index,
        row,
        col,
        grid: { left, right, top: gridTop, bottom: gridBottom },
        point: { x: gx, y: gy }
      });
      if (inCalendarSafeRegion({ cx: gx, cy: gy }, screenSize) && (!avoid || Math.hypot(gx - avoid.cx, gy - avoid.cy) > 20)) {
        await adbTap(gx, gy);
        await sleep(waitAfter);
        emitCalendarDebug(onDebug, { type: "grid_tap_no_header", point: { x: gx, y: gy } });
        return { x1: gx, y1: gy, x2: gx, y2: gy, cx: gx, cy: gy, method: "grid_calc_no_header" };
      }
    }

    // 4) Date-text node fallback from UI tree.
    const dateNodeCandidates = (lastNodes || [])
      .filter((n) => n && n.bounds)
      .map((n) => {
        const txt = `${n.text || ""}${n.contentDesc || ""}`;
        return {
          n,
          text: txt,
          score: dateNodeMatchScore(txt, ymd)
        };
      })
      .filter((x) => {
        if (x.score <= 0) return false;
        if (!inCalendarSafeRegion(x.n.bounds, screenSize)) return false;
        if (hasCalendarForbiddenText(x.text)) return false;
        return true;
      })
      .sort((a, b) => {
        if (b.score !== a.score) return b.score - a.score;
        if (a.n.bounds.cy !== b.n.bounds.cy) return a.n.bounds.cy - b.n.bounds.cy;
        return a.n.bounds.cx - b.n.bounds.cx;
      });
    emitCalendarDebug(onDebug, {
      type: "date_node_candidates",
      top: dateNodeCandidates.slice(0, 8).map((x) => ({
        score: x.score,
        text: String(x.text || "").slice(0, 44),
        bounds: x.n.bounds
      }))
    });
    if (dateNodeCandidates.length) {
      let targetNode = dateNodeCandidates[0].n;
      if (avoid) {
        const dist = (n) => Math.hypot(n.bounds.cx - avoid.cx, n.bounds.cy - avoid.cy);
        const alt = dateNodeCandidates.find((x) => dist(x.n) > 20);
        if (alt) targetNode = alt.n;
      }
      await adbTap(targetNode.bounds.cx, targetNode.bounds.cy);
      await sleep(waitAfter);
      emitCalendarDebug(onDebug, { type: "date_node_tap", point: { x: targetNode.bounds.cx, y: targetNode.bounds.cy }, chosen: targetNode.bounds });
      return { ...targetNode.bounds, method: "date_node_fallback" };
    }

    // 5) OCR text candidate fallback when grid mapping fails.
    const monthDay = `${ymd.m}月${ymd.d}`;
    const ymdSlash = `${ymd.y}/${ymd.m}/${ymd.d}`;
    const ymdDash = `${ymd.y}-${String(ymd.m).padStart(2, "0")}-${String(ymd.d).padStart(2, "0")}`;
    const cands = [];
    for (const w of words) {
      const t = String(w?.text || "").replace(/\s+/g, "");
      if (!t || hasCalendarForbiddenText(t)) continue;
      const bbox = w?.bbox || {};
      const x0 = Number(bbox.x0);
      const y0 = Number(bbox.y0);
      const x1 = Number(bbox.x1);
      const y1 = Number(bbox.y1);
      if (![x0, y0, x1, y1].every(Number.isFinite)) continue;
      const cx = (x0 + x1) / 2;
      const cy = (y0 + y1) / 2;
      if (!inCalendarSafeRegion({ cx, cy }, screenSize)) continue;

      let score = 0;
      if (t.includes(monthDay)) score += 120;
      if (t.includes(ymdSlash) || t.includes(ymdDash)) score += 110;
      if (t === String(day) || t === `${day}日`) score += 80;
      if (new RegExp(`(^|\\D)${day}(\\D|$)`).test(t)) score += 45;
      const centerBias = Math.abs(cx - screenSize.width / 2) / Math.max(1, screenSize.width / 2);
      score += Math.max(0, 12 - centerBias * 12);
      if (score > 0) cands.push({
        cx,
        cy,
        score,
        text: t.slice(0, 24),
        bounds: { x1, y1, x2, y2, cx, cy }
      });
    }

    if (cands.length) {
      cands.sort((a, b) => b.score - a.score);
      let target = cands[0];
      if (avoid) {
        const dist = (c) => Math.hypot(c.cx - avoid.cx, c.cy - avoid.cy);
        const alt = cands.find((c) => dist(c) > 24);
        if (alt) target = alt;
      }
      emitCalendarDebug(onDebug, {
        type: "ocr_candidates",
        top: cands.slice(0, 6),
        chosen: target
      });
      await adbTap(target.cx, target.cy);
      await sleep(waitAfter);
      return { x1: target.cx, y1: target.cy, x2: target.cx, y2: target.cy, cx: target.cx, cy: target.cy, method: "ocr_text" };
    }
  } finally {
    try {
      await fs.unlink(screenPath);
    } catch (_err) {
      // ignore
    }
  }

  emitCalendarDebug(onDebug, { type: "calendar_failed", date: dateStr });
  throw new Error(`calendar_date_not_found:${dateStr}`);
}

function execFileAsync(file, args, opts = {}) {
  const { signal, ...rest } = opts || {};
  return new Promise((resolve, reject) => {
    execFile(file, args, { windowsHide: true, ...rest, ...(signal ? { signal } : {}) }, (error, stdout, stderr) => {
      if (error) {
        error.stdout = stdout;
        error.stderr = stderr;
        reject(error);
      } else {
        resolve({ stdout: String(stdout || ""), stderr: String(stderr || "") });
      }
    });
  });
}

function parseDateHints(url) {
  const href = String(url || "");

  const sky = href.match(/\/(\d{6})\/(\d{6})\//);
  if (sky) {
    const dep = sky[1];
    const ret = sky[2];
    return {
      depMonth: String(Number(dep.slice(2, 4))),
      depDay: String(Number(dep.slice(4, 6))),
      retMonth: String(Number(ret.slice(2, 4))),
      retDay: String(Number(ret.slice(4, 6)))
    };
  }

  const pair =
    href.match(/(?:fromDate|depdate|date)=([0-9]{4}-[0-9]{2}-[0-9]{2})[,_]([0-9]{4}-[0-9]{2}-[0-9]{2})/i) ||
    href.match(/date=([0-9]{4}-[0-9]{2}-[0-9]{2}),([0-9]{4}-[0-9]{2}-[0-9]{2})/i);
  if (!pair) return null;

  const dep = pair[1].split("-");
  const ret = pair[2].split("-");
  return {
    depMonth: String(Number(dep[1])),
    depDay: String(Number(dep[2])),
    retMonth: String(Number(ret[1])),
    retDay: String(Number(ret[2]))
  };
}

function collectAmounts(text, pattern) {
  pattern.lastIndex = 0;
  const out = [];
  let m;
  while ((m = pattern.exec(text)) !== null) {
    const amount = Number(String(m[1] || "").replace(/,/g, ""));
    if (Number.isFinite(amount) && amount >= 200 && amount <= 99999) {
      out.push(amount);
    }
  }
  return out;
}

function extractPrice(text, meta = {}) {
  const normalized = normalizeText(text);
  if (!normalized) {
    return { ok: false, reason: "empty_text" };
  }

  if (/(未找到符合|暂无结果|暂时无法查询|no flights?|no result|sold out)/i.test(normalized)) {
    return { ok: false, reason: "no_result" };
  }

  const dateHint = parseDateHints(meta.url);
  if (dateHint) {
    const dateScoped = new RegExp(
      `${dateHint.depMonth}\\s*月\\s*${dateHint.depDay}\\s*日[\\s\\S]{0,60}${dateHint.retMonth}\\s*月\\s*${dateHint.retDay}\\s*日[\\s\\S]{0,30}[¥￥]?\\s*([0-9][0-9,]{2,7})`,
      "g"
    );
    const byDate = collectAmounts(normalized, dateScoped);
    if (byDate.length > 0) {
      const amount = Math.min(...byDate);
      return { ok: true, amount, display: `¥${amount.toLocaleString("zh-CN")}` };
    }
  }

  const preferred = [
    /(?:最便宜|最低价?|低至|往返(?:总价|最低)?|含税(?:总价)?|综合最佳)[^\d¥￥]{0,14}[¥￥]?\s*([0-9][0-9,]{2,7})/gi,
    /(?:Cheapest|Best|Lowest)[^\d¥￥]{0,14}[¥￥]?\s*([0-9][0-9,]{2,7})/gi,
    /[¥￥]\s*([0-9][0-9,]{2,7})\s*(?:起|起价)?/g
  ];

  const candidates = [];
  for (const reg of preferred) {
    candidates.push(...collectAmounts(normalized, reg));
  }

  if (!candidates.length) {
    const broad = /(?:机票|航班|flight|trip|总价|往返)[^\n¥￥]{0,30}[¥￥]?\s*([0-9][0-9,]{2,7})/gi;
    candidates.push(...collectAmounts(normalized, broad));
  }

  if (!candidates.length) {
    return { ok: false, reason: "price_not_found" };
  }

  const roundTripLike = /(roundtrip|round-|rtn=1|toDate=|date=[0-9]{4}-[0-9]{2}-[0-9]{2},[0-9]{4}-[0-9]{2}-[0-9]{2}|往返)/i.test(String(meta.url || ""));
  const floor = roundTripLike ? 300 : 200;
  const ceiling = roundTripLike ? 20000 : 99999;
  const filtered = candidates.filter((n) => n >= floor && n <= ceiling);
  const amount = Math.min(...(filtered.length ? filtered : candidates));
  return { ok: true, amount, display: `¥${amount.toLocaleString("zh-CN")}` };
}

function extractAmountsGeneric(text) {
  const normalized = normalizeText(text);
  if (!normalized) return [];
  const reg = /(?:^|[^\d,])(?:¥|￥)?\s*([1-9]\d{2,7}(?:,\d{3})*)(?:\s*元)?(?!\d)/g;
  const out = [];
  let m;
  while ((m = reg.exec(normalized)) !== null) {
    const amount = Number(String(m[1] || "").replace(/,/g, ""));
    if (Number.isFinite(amount) && amount >= 200 && amount <= 20000) {
      out.push(amount);
    }
  }
  return out;
}

function parseRegionText(regionText = "") {
  const parts = String(regionText || "").split(",").map((x) => Number(String(x || "").trim()));
  if (parts.length !== 4 || parts.some((x) => !Number.isFinite(x))) return null;
  const [x1, y1, x2, y2] = parts;
  return {
    x1: Math.min(x1, x2),
    y1: Math.min(y1, y2),
    x2: Math.max(x1, x2),
    y2: Math.max(y1, y2)
  };
}

function extractAmountsFromOcrWords(words = [], region = null) {
  const out = [];
  const cleanToken = (text) => String(text || "").replace(/\s+/g, "");
  const parseAmount = (text) => {
    const m = String(text || "").match(/(?:[¥￥+])?\s*([1-9]\d{2,5}(?:,\d{3})*)/);
    if (!m) return null;
    const amount = Number(String(m[1]).replace(/,/g, ""));
    if (!Number.isFinite(amount) || amount < 200 || amount > 20000) return null;
    return amount;
  };
  const pushCandidate = (rawText, bounds, boost = 0) => {
    const t = cleanToken(rawText);
    if (!t) return;
    const amount = parseAmount(t);
    if (!amount) return;
    const x1 = Number(bounds?.x1), y1 = Number(bounds?.y1), x2 = Number(bounds?.x2), y2 = Number(bounds?.y2);
    if (![x1, y1, x2, y2].every(Number.isFinite)) return;
    const cx = (x1 + x2) / 2;
    const cy = (y1 + y2) / 2;
    if (region) {
      if (cx < region.x1 || cx > region.x2 || cy < region.y1 || cy > region.y2) return;
    }
    const score = (/[¥￥]/.test(t) ? 30 : 0) + Math.max(0, 14 - Math.abs(cx - 600) / 120) + boost;
    out.push({
      text: t.slice(0, 48),
      amount,
      score,
      bounds: { x1, y1, x2, y2, cx, cy }
    });
  };

  const parsedWords = [];
  for (const w of (Array.isArray(words) ? words : [])) {
    const t = cleanToken(w?.text || "");
    const bbox = w?.bbox || {};
    const x0 = Number(bbox.x0), y0 = Number(bbox.y0), x1 = Number(bbox.x1), y1 = Number(bbox.y1);
    if (!t || ![x0, y0, x1, y1].every(Number.isFinite)) continue;
    parsedWords.push({ text: t, x1: x0, y1: y0, x2: x1, y2: y1, cx: (x0 + x1) / 2, cy: (y0 + y1) / 2 });
    pushCandidate(t, { x1: x0, y1: y0, x2: x1, y2: y1 }, 0);
  }

  // Fallback for OCR split tokens like "¥" + "1050" or "1" "0" "5" "0".
  const sorted = parsedWords.slice().sort((a, b) => a.cy - b.cy || a.cx - b.cx);
  const lines = [];
  for (const w of sorted) {
    const h = Math.max(8, w.y2 - w.y1);
    let line = null;
    for (const l of lines) {
      const tol = Math.max(12, Math.max(l.avgH, h) * 0.75);
      if (Math.abs(w.cy - l.cy) <= tol) {
        line = l;
        break;
      }
    }
    if (!line) {
      line = { words: [], cy: w.cy, avgH: h };
      lines.push(line);
    }
    line.words.push(w);
    line.cy = (line.cy * (line.words.length - 1) + w.cy) / line.words.length;
    line.avgH = (line.avgH * (line.words.length - 1) + h) / line.words.length;
  }

  for (const line of lines) {
    const ws = line.words.sort((a, b) => a.cx - b.cx);
    for (let i = 0; i < ws.length; i += 1) {
      let joined = "";
      let bx1 = Infinity;
      let by1 = Infinity;
      let bx2 = -Infinity;
      let by2 = -Infinity;
      for (let j = i; j < Math.min(ws.length, i + 6); j += 1) {
        const w = ws[j];
        joined += w.text;
        bx1 = Math.min(bx1, w.x1);
        by1 = Math.min(by1, w.y1);
        bx2 = Math.max(bx2, w.x2);
        by2 = Math.max(by2, w.y2);
        pushCandidate(joined, { x1: bx1, y1: by1, x2: bx2, y2: by2 }, 5);
      }
    }
  }

  const uniq = new Map();
  for (const c of out) {
    const k = `${Math.round(c.bounds.x1)}_${Math.round(c.bounds.y1)}_${Math.round(c.bounds.x2)}_${Math.round(c.bounds.y2)}_${c.amount}`;
    const prev = uniq.get(k);
    if (!prev || c.score > prev.score) uniq.set(k, c);
  }
  const result = Array.from(uniq.values());
  result.sort((a, b) => b.score - a.score || a.amount - b.amount);
  return result;
}

function extractNumericWordHints(words = [], region = null, targetAmounts = []) {
  const targets = (Array.isArray(targetAmounts) ? targetAmounts : [])
    .map((x) => Number(x))
    .filter((x) => Number.isFinite(x) && x > 0);
  const out = [];
  for (const w of (Array.isArray(words) ? words : [])) {
    const textRaw = String(w?.text || "");
    const text = textRaw.replace(/\s+/g, "");
    if (!text) continue;
    const bbox = w?.bbox || {};
    const x0 = Number(bbox.x0), y0 = Number(bbox.y0), x1 = Number(bbox.x1), y1 = Number(bbox.y1);
    if (![x0, y0, x1, y1].every(Number.isFinite)) continue;
    const cx = (x0 + x1) / 2;
    const cy = (y0 + y1) / 2;
    if (region) {
      if (cx < region.x1 || cx > region.x2 || cy < region.y1 || cy > region.y2) continue;
    }
    if (!/[¥￥]?\+?\d{2,6}(?:,\d{3})?/.test(text)) continue;
    const m = text.match(/([1-9]\d{1,6}(?:,\d{3})*)/);
    if (!m) continue;
    const amount = Number(String(m[1]).replace(/,/g, ""));
    if (!Number.isFinite(amount) || amount <= 0) continue;
    const amountDiff = targets.length ? Math.min(...targets.map((t) => Math.abs(t - amount))) : 99999;
    const score = (/[¥￥]/.test(text) ? 30 : 0) + Math.max(0, 16 - Math.min(16, amountDiff / 120));
    out.push({
      text: text.slice(0, 40),
      amount,
      score,
      bounds: { x1: x0, y1: y0, x2: x1, y2: y1, cx, cy }
    });
  }
  out.sort((a, b) => b.score - a.score || a.amount - b.amount);
  return out.slice(0, 16);
}

function extractPriceUiHints(nodes = [], region = null, targetAmounts = []) {
  const targets = (Array.isArray(targetAmounts) ? targetAmounts : [])
    .map((x) => Number(x))
    .filter((x) => Number.isFinite(x) && x > 0);
  const out = [];
  for (const n of (Array.isArray(nodes) ? nodes : [])) {
    const b = n?.bounds;
    if (!b) continue;
    const cx = Number(b.cx), cy = Number(b.cy);
    if (!Number.isFinite(cx) || !Number.isFinite(cy)) continue;
    if (region) {
      if (cx < region.x1 || cx > region.x2 || cy < region.y1 || cy > region.y2) continue;
    }
    const t = `${n?.text || ""}${n?.contentDesc || ""}`.replace(/\s+/g, "");
    if (!t) continue;
    const m = t.match(/(?:[¥￥+])?\s*([1-9]\d{2,6}(?:,\d{3})*)/);
    if (!m) continue;
    const amount = Number(String(m[1]).replace(/,/g, ""));
    if (!Number.isFinite(amount) || amount < 200 || amount > 20000) continue;
    const amountDiff = targets.length ? Math.min(...targets.map((x) => Math.abs(x - amount))) : 99999;
    const score = (/[¥￥]/.test(t) ? 30 : 0) + (n?.clickable ? 6 : 0) + Math.max(0, 16 - Math.min(16, amountDiff / 120));
    out.push({
      text: t.slice(0, 40),
      amount,
      score,
      bounds: { x1: b.x1, y1: b.y1, x2: b.x2, y2: b.y2, cx, cy }
    });
  }
  out.sort((a, b) => b.score - a.score || a.amount - b.amount);
  return out.slice(0, 16);
}

function getDefaultWorkflows() {
  return [
    {
      id: "ctrip",
      name: "携程App机票流程",
      packageName: "ctrip.android.view",
      enabled: true,
      steps: []
    },
    {
      id: "qunar",
      name: "去哪儿App机票流程",
      packageName: "com.Qunar",
      enabled: true,
      steps: []
    },
    {
      id: "tongcheng",
      name: "同程App机票流程",
      packageName: "com.tongcheng.android",
      enabled: true,
      steps: []
    },
    {
      id: "skyscanner",
      name: "天巡浏览器流程",
      packageName: MOBILE_BROWSER_PACKAGE,
      enabled: true,
      steps: []
    }
  ];
}

async function readBodyText(page) {
  for (let i = 0; i < 5; i += 1) {
    try {
      await page.waitForLoadState("domcontentloaded", { timeout: 4000 });
      const text = await page.evaluate(() => document.body?.innerText || "");
      if (text) return text;
    } catch (_err) {
      await page.waitForTimeout(700);
    }
  }
  return "";
}

async function fetchTextWithTimeout(url, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(url, { signal: controller.signal });
    if (!resp.ok) return "";
    return await resp.text();
  } catch (_err) {
    return "";
  } finally {
    clearTimeout(timer);
  }
}

async function scrapePriceFromBrowser(context, url, manualWaitMs = DEFAULT_MANUAL_CHALLENGE_WAIT_MS) {
  const page = await context.newPage();
  try {
    await page.bringToFront();
    await page.goto(url, { timeout: NAV_TIMEOUT_MS, waitUntil: "domcontentloaded" });
    await page.waitForTimeout(VISIBLE_WAIT_MS);

    let text = await readBodyText(page);
    const currentUrl = page.url();
    const lowerText = (text || "").toLowerCase();
    const lowerUrl = (currentUrl || "").toLowerCase();
    const needsManualCheck =
      /captcha|robot|person or a robot|人机|验证|滑块/.test(lowerText) ||
      /captcha|sttc\/px/.test(lowerUrl);

    if (needsManualCheck) {
      if (manualWaitMs > 0) {
        await page.bringToFront();
        await page.waitForTimeout(manualWaitMs);
        text = await readBodyText(page);
        const afterUrl = page.url();
        if (/captcha|sttc\/px/.test((afterUrl || "").toLowerCase())) {
          return { ok: false, reason: "captcha_required" };
        }
      } else {
        return { ok: false, reason: "captcha_required" };
      }
    }

    const result = extractPrice(text, { url });
    if (result.ok) return result;

    try {
      const jinaUrl = `https://r.jina.ai/http://${url.replace(/^https?:\/\//, "")}`;
      const mirrorText = await fetchTextWithTimeout(jinaUrl, 6000);
      if (mirrorText) {
        const mirrorResult = extractPrice(mirrorText, { url });
        if (mirrorResult.ok) return { ...mirrorResult, source: "mirror_fallback" };
      }
    } catch (_err) {
      // ignore mirror errors
    }

    return {
      ok: false,
      reason: result.reason || "unknown",
      snippet: normalizeText(text).slice(0, 220)
    };
  } finally {
    await page.close();
  }
}

function adbArgs(args) {
  if (ADB_DEVICE) {
    return ["-s", ADB_DEVICE, ...args];
  }
  return args;
}

async function runAdb(args, timeout = 30000) {
  const signal = getActiveWorkflowSignal();
  return execFileAsync(ADB_PATH, adbArgs(args), { timeout, signal });
}

function quoteForAdbShell(raw) {
  const str = String(raw || "");
  // adb shell uses /system/bin/sh parsing; single-quote the whole token.
  return `'${str.replace(/'/g, `'\"'\"'`)}'`;
}

function inferMobileTarget(url) {
  const u = String(url || "").toLowerCase();
  if (u.includes("tianxun.com") || u.includes("skyscanner")) {
    return { channel: "browser", packageName: MOBILE_BROWSER_PACKAGE, label: "Skyscanner(Browser)" };
  }
  if (u.includes("ctrip.com")) {
    return { channel: "app", packageName: "ctrip.android.view", label: "携程App" };
  }
  if (u.includes("ly.com")) {
    return { channel: "app", packageName: "com.tongcheng.android", label: "同程App" };
  }
  if (u.includes("qunar.com")) {
    return { channel: "app", packageName: "com.Qunar", label: "去哪儿App" };
  }
  return { channel: "browser", packageName: MOBILE_BROWSER_PACKAGE, label: "Browser(Default)" };
}

async function adbTap(x, y) {
  await runAdb(["shell", "input", "tap", String(Math.round(x)), String(Math.round(y))], 8000);
}

async function getScreenSize() {
  const { stdout } = await runAdb(["shell", "wm", "size"], 8000);
  const m = String(stdout || "").match(/Physical size:\s*(\d+)x(\d+)/i);
  return {
    width: m ? Number(m[1]) : 1200,
    height: m ? Number(m[2]) : 2640
  };
}

async function adbTapRatio(rx, ry) {
  const s = await getScreenSize();
  await adbTap(s.width * rx, s.height * ry);
}

async function adbBack() {
  await runAdb(["shell", "input", "keyevent", "4"], 8000);
}

async function adbEnter() {
  await runAdb(["shell", "input", "keyevent", "66"], 8000);
}

async function adbMoveEnd() {
  await runAdb(["shell", "input", "keyevent", "123"], 8000);
}

async function adbDeleteChars(times = 12) {
  for (let i = 0; i < times; i += 1) {
    await runAdb(["shell", "input", "keyevent", "67"], 4000);
  }
}

async function adbInputText(txt) {
  const safe = String(txt || "").replace(/ /g, "%s");
  if (!safe) return;
  await runAdb(["shell", "input", "text", safe], 12000);
}

async function adbPasteText(txt) {
  const content = String(txt || "");
  if (!content) return false;
  const clipboardOutputLooksFailed = (out = "", err = "") => {
    const s = `${out || ""}\n${err || ""}`.toLowerCase();
    return (
      s.includes("no shell command implementation") ||
      s.includes("no service specified") ||
      s.includes("unknown command") ||
      s.includes("exception")
    );
  };
  try {
    const r = await runAdb(["shell", "cmd", "clipboard", "set", "text", content], 12000);
    if (clipboardOutputLooksFailed(r?.stdout, r?.stderr)) {
      throw new Error("clipboard_cmd_not_supported");
    }
  } catch (_err) {
    try {
      const cmd = `cmd clipboard set text ${quoteForAdbShell(content)}`;
      const r2 = await runAdb(["shell", "sh", "-c", cmd], 12000);
      if (clipboardOutputLooksFailed(r2?.stdout, r2?.stderr)) {
        throw new Error("clipboard_sh_not_supported");
      }
    } catch (_err2) {
      return false;
    }
  }
  try {
    await runAdb(["shell", "input", "keyevent", "279"], 8000); // KEYCODE_PASTE
    return true;
  } catch (_err3) {
    return false;
  }
}

function toAsciiCityFallback(text, vars = {}, opts = {}) {
  const t = String(text || "").trim();
  if (!t) return "";
  const cityMap = {
    "北京": "beijing",
    "福州": "fuzhou",
    "三亚": "sanya",
    "重庆": "chongqing",
    "海口": "haikou",
    "桂林": "guilin",
    "佛山": "foshan",
    "昆明": "kunming",
    "平潭": "pingtan",
    "大理": "dali",
    "大连": "dalian",
    "呼和浩特": "hohhot",
    "日照": "rizhao",
    "青岛": "qingdao"
  };
  if (cityMap[t]) return cityMap[t];
  if (opts && opts.allowCodeFallback && vars.toCode) return String(vars.toCode).toLowerCase();
  return "";
}

function parseBounds(boundsText) {
  const m = String(boundsText || "").match(/\[(\d+),(\d+)\]\[(\d+),(\d+)\]/);
  if (!m) return null;
  const x1 = Number(m[1]);
  const y1 = Number(m[2]);
  const x2 = Number(m[3]);
  const y2 = Number(m[4]);
  return { x1, y1, x2, y2, cx: (x1 + x2) / 2, cy: (y1 + y2) / 2 };
}

function parseUiNodes(xml) {
  const nodes = [];
  const reg = /<node\b([^>]*?)\/>/g;
  let m;
  while ((m = reg.exec(xml)) !== null) {
    const attrs = m[1];
    const text = (attrs.match(/text="([^"]*)"/) || [])[1] || "";
    const contentDesc = (attrs.match(/content-desc="([^"]*)"/) || [])[1] || "";
    const bounds = (attrs.match(/bounds="([^"]*)"/) || [])[1] || "";
    const clickable = ((attrs.match(/clickable="([^"]*)"/) || [])[1] || "") === "true";
    const enabled = ((attrs.match(/enabled="([^"]*)"/) || [])[1] || "") === "true";
    const b = parseBounds(bounds);
    nodes.push({ text, contentDesc, bounds: b, clickable, enabled });
  }
  return nodes.filter((n) => n.bounds);
}

async function dumpUiNodes(tag = "ui") {
  const remote = `/sdcard/${tag}_${Date.now()}.xml`;
  const local = path.join(MOBILE_TMP_DIR, path.basename(remote));
  await runAdb(["shell", "uiautomator", "dump", remote], 15000);
  await runAdb(["pull", remote, local], 15000);
  const xml = await fs.readFile(local, "utf8");
  try {
    await fs.unlink(local);
  } catch (_err) {
    // ignore
  }
  try {
    await runAdb(["shell", "rm", remote], 10000);
  } catch (_err) {
    // ignore
  }
  return parseUiNodes(xml);
}

function findNodeByText(nodes, words) {
  const arr = Array.isArray(words) ? words : [words];
  for (const w of arr) {
    const ww = String(w || "").trim();
    if (!ww) continue;
    const found = nodes.find((n) => (n.text && n.text.includes(ww)) || (n.contentDesc && n.contentDesc.includes(ww)));
    if (found) return found;
  }
  return null;
}

async function tapNodeByText(words, waitAfter = MOBILE_STEP_WAIT_MS, retries = 2) {
  for (let i = 0; i <= retries; i += 1) {
    const nodes = await dumpUiNodes("tap_text");
    const n = findNodeByText(nodes, words);
    if (n && n.bounds) {
      await adbTap(n.bounds.cx, n.bounds.cy);
      await sleep(waitAfter);
      return true;
    }
    await sleep(500);
  }
  return false;
}

async function fillCityByCode(code) {
  const okSearch = await tapNodeByText(["搜索", "请输入", "城市", "目的地"], 600, 1);
  if (okSearch) {
    await adbMoveEnd();
    await adbDeleteChars(16);
  }
  await adbInputText(code);
  await sleep(900);
  await adbEnter();
  await sleep(1400);
  await tapNodeByText([code, "三亚", "重庆", "海口", "福州", "昆明", "桂林"], 1000, 0);
}

function dayFromDate(yyyyMmDd) {
  const m = String(yyyyMmDd || "").match(/^\d{4}-\d{2}-(\d{2})$/);
  if (!m) return "";
  return String(Number(m[1]));
}

async function chooseDates(depDate, retDate) {
  const depDay = dayFromDate(depDate);
  const retDay = dayFromDate(retDate);
  if (!depDay || !retDay) return false;
  await tapNodeByText([depDay], 700, 1);
  await tapNodeByText([retDay], 900, 1);
  await tapNodeByText(["确定", "完成", "返回", "查询"], 900, 0);
  return true;
}

async function runAppSearchFlow(packageName, trip) {
  await runAdb(["shell", "monkey", "-p", packageName, "-c", "android.intent.category.LAUNCHER", "1"], 12000);
  await sleep(1400);

  // Common dismiss actions.
  await tapNodeByText(["关闭", "跳过", "以后再说", "知道了", "×", "x"], 500, 0);

  const tappedFlight = await tapNodeByText(["机票", "低价机票", "飞机"], 1200, 2);
  if (!tappedFlight) {
    if (packageName === "ctrip.android.view") await adbTapRatio(0.48, 0.16);
    if (packageName === "com.Qunar") await adbTapRatio(0.14, 0.37);
    if (packageName === "com.tongcheng.android") await adbTapRatio(0.14, 0.37);
    await sleep(1200);
  }
  const tappedRound = await tapNodeByText(["往返"], 600, 2);
  if (!tappedRound) {
    await adbTapRatio(0.5, packageName === "ctrip.android.view" ? 0.24 : 0.29);
    await sleep(700);
  }

  // Select destination city field first, then search by airport code.
  const tappedDest = await tapNodeByText(["上海", "选择目的地", "到达地", "目的地"], 800, 2);
  if (!tappedDest) {
    await adbTapRatio(0.82, packageName === "ctrip.android.view" ? 0.34 : 0.37);
    await sleep(900);
  }
  if (trip.toCode) {
    await fillCityByCode(trip.toCode);
  }

  // Select dates.
  const tappedDate = await tapNodeByText(["日期", "出发日期", "3月", "去程"], 900, 2);
  if (!tappedDate) {
    await adbTapRatio(0.3, packageName === "ctrip.android.view" ? 0.42 : 0.45);
    await sleep(900);
  }
  await chooseDates(trip.depDate, trip.retDate);

  // Final search.
  const tappedSearch = await tapNodeByText(["查询", "搜索", "查机票"], 2200, 3);
  if (!tappedSearch) {
    await adbTapRatio(0.5, packageName === "ctrip.android.view" ? 0.54 : 0.66);
    await sleep(2200);
  }
}

function buildFallbackTarget(primary) {
  if (primary && primary.channel === "app") {
    return { channel: "browser", packageName: MOBILE_BROWSER_PACKAGE, label: `${primary.label}->BrowserFallback` };
  }
  return null;
}

async function ensureMobileReady() {
  await fs.mkdir(MOBILE_TMP_DIR, { recursive: true });
  await runAdb(["start-server"], 10000);
  const { stdout } = await runAdb(["devices", "-l"], 10000);
  const ready = stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => /\bdevice\b/.test(line) && !line.startsWith("List of devices"));
  if (!ready.length) {
    throw new Error("no_mobile_device: adb did not find an online device");
  }
}

async function getOcrWorker() {
  if (!ocrWorkerPromise) {
    ocrWorkerPromise = (async () => {
      const worker = await createWorker("eng");
      await worker.setParameters({
        tessedit_pageseg_mode: "6",
        preserve_interword_spaces: "1"
      });
      return worker;
    })();
  }
  return ocrWorkerPromise;
}

async function ocrImage(localImagePath) {
  const worker = await getOcrWorker();
  const out = await worker.recognize(localImagePath);
  return out?.data?.text || "";
}

async function ocrWords(localImagePath) {
  const worker = await getOcrWorker();
  const out = await worker.recognize(localImagePath);
  return Array.isArray(out?.data?.words) ? out.data.words : [];
}

async function preprocessImageForOcr(localImagePath) {
  const ext = path.extname(localImagePath) || ".png";
  const target = localImagePath.replace(new RegExp(`${ext}$`), `_ocr${ext}`);
  const input = sharp(localImagePath);
  const meta = await input.metadata();
  const width = Number(meta.width || 1200);
  const targetWidth = Math.min(2600, Math.max(1600, width * 2));
  await input
    .resize({ width: targetWidth })
    .grayscale()
    .normalize()
    .sharpen()
    .linear(1.18, -12)
    .threshold(158)
    .toFile(target);
  return target;
}

async function captureMobilePage(url, forcedTarget = null) {
  const remoteName = `price_agent_${Date.now()}_${Math.floor(Math.random() * 10000)}.png`;
  const remotePath = `/sdcard/${remoteName}`;
  const localPath = path.join(MOBILE_TMP_DIR, remoteName);
  const target = forcedTarget || inferMobileTarget(url);
  const trip = parseTripFromUrl(url);

  try {
    if (target.channel === "app") {
      await runAppSearchFlow(target.packageName, trip);
    } else {
      // Put browser in foreground then open URL.
      await runAdb(["shell", "monkey", "-p", target.packageName, "-c", "android.intent.category.LAUNCHER", "1"], 12000);
      await sleep(900);
      const safeUrl = quoteForAdbShell(url);
      await runAdb(
        ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", safeUrl, target.packageName],
        20000
      );
      await sleep(Math.max(3000, MOBILE_PAGE_WAIT_MS));
    }
  } catch (_err) {
    if (target.channel === "app") {
      throw new Error(`app_flow_failed:${target.packageName}:${String(_err?.message || _err)}`);
    }
    const safeUrl = quoteForAdbShell(url);
    await runAdb(["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", safeUrl, target.packageName], 20000);
    await sleep(Math.max(3000, MOBILE_PAGE_WAIT_MS));
  }
  await runAdb(["shell", "screencap", "-p", remotePath], 30000);
  await runAdb(["pull", remotePath, localPath], 30000);
  try {
    await runAdb(["shell", "rm", remotePath], 10000);
  } catch (_err) {
    // ignore cleanup error
  }
  return { localPath, target };
}

async function captureCurrentMobileScreen(label = "workflow") {
  const remoteName = `${label}_${Date.now()}_${Math.floor(Math.random() * 10000)}.png`;
  const remotePath = `/sdcard/${remoteName}`;
  const localPath = path.join(MOBILE_TMP_DIR, remoteName);
  await runAdb(["shell", "screencap", "-p", remotePath], 30000);
  await runAdb(["pull", remotePath, localPath], 30000);
  try {
    await runAdb(["shell", "rm", remotePath], 10000);
  } catch (_err) {
    // ignore
  }
  return localPath;
}

async function ocrImageMaybeRegion(localPath, regionText = "") {
  const region = String(regionText || "").trim();
  if (!region) return ocrImage(localPath);

  const parts = region.split(",").map((x) => Number(x.trim()));
  if (parts.length !== 4 || parts.some((x) => !Number.isFinite(x))) {
    return ocrImage(localPath);
  }
  const [x1, y1, x2, y2] = parts;
  const left = Math.max(0, Math.min(x1, x2));
  const top = Math.max(0, Math.min(y1, y2));
  const width = Math.max(1, Math.abs(x2 - x1));
  const height = Math.max(1, Math.abs(y2 - y1));
  const regionPath = localPath.replace(/\.png$/i, "_region.png");
  try {
    await sharp(localPath).extract({ left: Math.round(left), top: Math.round(top), width: Math.round(width), height: Math.round(height) }).toFile(regionPath);
    return await ocrImage(regionPath);
  } finally {
    try {
      await fs.unlink(regionPath);
    } catch (_err) {
      // ignore
    }
  }
}

async function captureBrowserScreenshot(page, label = "browser_ocr") {
  const fileName = `${label}_${Date.now()}_${Math.floor(Math.random() * 10000)}.png`;
  const localPath = path.join(MOBILE_TMP_DIR, fileName);
  await fs.mkdir(MOBILE_TMP_DIR, { recursive: true });
  const buffer = await page.screenshot({ fullPage: false });
  await fs.writeFile(localPath, buffer);
  const meta = await sharp(buffer).metadata();
  return {
    localPath,
    buffer,
    width: Number(meta.width || 0),
    height: Number(meta.height || 0)
  };
}

async function ocrTextFromImagePath(localImagePath) {
  let preprocessedPath = "";
  try {
    preprocessedPath = await preprocessImageForOcr(localImagePath);
    const text = `${await ocrImage(localImagePath)}\n${await ocrImage(preprocessedPath)}`;
    return text;
  } finally {
    if (preprocessedPath) {
      try {
        await fs.unlink(preprocessedPath);
      } catch (_err) {
        // ignore
      }
    }
  }
}

async function loadWorkflows() {
  const data = await readJsonFileSafe(WORKFLOW_FILE, null);
  if (Array.isArray(data?.workflows)) return data;
  const defaults = { workflows: getDefaultWorkflows(), updatedAt: Date.now() };
  await writeJsonFileSafe(WORKFLOW_FILE, defaults);
  return defaults;
}

async function saveWorkflows(workflows) {
  const payload = {
    workflows: Array.isArray(workflows) ? workflows : [],
    updatedAt: Date.now()
  };
  await writeJsonFileSafe(WORKFLOW_FILE, payload);
  return payload;
}

async function appendPriceHistory(entry) {
  const normalized = normalizeHistoryRecord(entry);
  if (!normalized) {
    throw new Error("invalid_history_entry_or_unknown_platform");
  }
  const current = await readJsonFileSafe(PRICE_HISTORY_FILE, { records: [] });
  const records = Array.isArray(current.records) ? current.records : [];
  records.unshift({
    id: `${Date.now()}_${Math.floor(Math.random() * 10000)}`,
    ...normalized,
    createdAt: Date.now()
  });
  const trimmed = records.slice(0, 500);
  const payload = { records: trimmed, updatedAt: Date.now() };
  await writeJsonFileSafe(PRICE_HISTORY_FILE, payload);
  return payload;
}

async function deletePriceHistoryById(id) {
  const targetId = String(id || "").trim();
  if (!targetId) return { payload: await readJsonFileSafe(PRICE_HISTORY_FILE, { records: [] }), removed: 0 };
  const current = await readJsonFileSafe(PRICE_HISTORY_FILE, { records: [] });
  const records = Array.isArray(current.records) ? current.records : [];
  const buildLegacyId = (r) => {
    const category = String(r?.category || "flight").trim();
    const platform = String(r?.platformCode || r?.platform || r?.platformLabel || "").trim();
    const amount = String(Number(r?.amount || 0));
    const createdAt = String(Number(r?.createdAt || r?.updatedAt || 0));
    const depTime = String(r?.depTime || "").trim();
    const arrTime = String(r?.arrTime || "").trim();
    const flightNo = String(r?.flightNo || "").trim();
    const checkinDate = String(r?.checkinDate || "").trim();
    const hotelName = String(r?.hotelName || "").trim();
    return `legacy:${category}|${platform}|${amount}|${createdAt}|${depTime}|${arrTime}|${flightNo}|${checkinDate}|${hotelName}`;
  };
  const next = records.filter((r) => {
    const idMatch = String(r?.id || "") === targetId;
    const legacyMatch = buildLegacyId(r) === targetId;
    return !(idMatch || legacyMatch);
  });
  const removed = Math.max(0, records.length - next.length);
  const payload = { records: next, updatedAt: Date.now() };
  await writeJsonFileSafe(PRICE_HISTORY_FILE, payload);
  return { payload, removed };
}

async function captureScreenAsBase64(label = "workflow_debug") {
  const imgPath = await captureCurrentMobileScreen(label);
  try {
    const buf = await fs.readFile(imgPath);
    const meta = await sharp(buf).metadata();
    return {
      image_base64: buf.toString("base64"),
      width: Number(meta.width || 0),
      height: Number(meta.height || 0)
    };
  } finally {
    try {
      await fs.unlink(imgPath);
    } catch (_err) {
      // ignore
    }
  }
}

async function captureScreenWithOcr(label = "workflow_result") {
  const imgPath = await captureCurrentMobileScreen(label);
  let preprocessedPath = "";
  try {
    const buf = await fs.readFile(imgPath);
    const meta = await sharp(buf).metadata();
    preprocessedPath = await preprocessImageForOcr(imgPath);
    const text = `${await ocrImage(imgPath)}\n${await ocrImage(preprocessedPath)}`;
    const amounts = extractAmountsGeneric(text).filter((x) => Number.isFinite(x) && x >= 200 && x <= 20000);
    const best = extractPrice(text, { url: "mobile_workflow_screen" });
    return {
      screen: {
        image_base64: buf.toString("base64"),
        width: Number(meta.width || 0),
        height: Number(meta.height || 0)
      },
      amounts,
      bestAmount: best.ok && Number.isFinite(best.amount) ? Number(best.amount) : null,
      ocrTextLength: String(text || "").length
    };
  } finally {
    try {
      if (preprocessedPath) await fs.unlink(preprocessedPath);
    } catch (_err) {
      // ignore
    }
    try {
      await fs.unlink(imgPath);
    } catch (_err) {
      // ignore
    }
  }
}

async function runVlmWorkflow(workflow, context = {}, options = {}) {
  const steps = Array.isArray(workflow?.steps) ? workflow.steps : [];
  const priceCandidates = [];
  const traces = [];
  const debugEvents = [];
  const debugEnabled = !!options.debug;
  const postRunOcrEnabled = options.postRunOcr !== false;
  const abortSignal = options.signal || null;
  let lastScreenPath = "";
  const vars = buildWorkflowVars(context);
  const hasRoundTrip = !!(context && context.depDate && context.retDate && String(context.depDate) !== String(context.retDate));
  const candidateFloor = hasRoundTrip ? 600 : 200;
  const ensureNotAborted = () => {
    if (abortSignal?.aborted) throw buildWorkflowAbortError();
  };
  const pushDebug = (payload) => {
    if (!debugEnabled) return;
    debugEvents.push({ ts: Date.now(), ...payload });
  };

  try {
    ensureNotAborted();
    for (let i = 0; i < steps.length; i += 1) {
      ensureNotAborted();
      const step = steps[i] || {};
      if (step.enabled === false) continue;
      const action = String(step.action || "").trim();
      const waitMs = Math.max(0, parseNum(step.waitMs, MOBILE_STEP_WAIT_MS));
      const repeat = Math.max(1, parseNum(step.repeat, 1));
      traces.push({ index: i, action, name: String(step.name || "") });
      pushDebug({ type: "step_start", stepIndex: i, name: String(step.name || ""), action, repeat, waitMs });

      for (let r = 0; r < repeat; r += 1) {
        ensureNotAborted();
        if (action === "launch_app") {
          const pkg = resolveTemplateText(step.packageName || workflow.packageName || "", vars).trim();
          if (!pkg) throw new Error(`step_${i}_missing_package`);
          await runAdb(["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"], 12000);
        } else if (action === "open_url") {
          const pkg = resolveTemplateText(step.packageName || workflow.packageName || MOBILE_BROWSER_PACKAGE, vars).trim();
          const url = resolveTemplateText(step.url || context.url || "", vars).trim();
          if (!url) throw new Error(`step_${i}_missing_url`);
          const safeUrl = quoteForAdbShell(url);
          await runAdb(["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", safeUrl, pkg], 20000);
        } else if (action === "tap") {
          const p = parseCoordPair(step.fromCoord || `${step.x || ""},${step.y || ""}`);
          if (!p) throw new Error(`step_${i}_invalid_tap_coord`);
          await adbTap(p.x, p.y);
        } else if (action === "swipe") {
          const from = parseCoordPair(step.fromCoord || `${step.x1 || ""},${step.y1 || ""}`);
          const to = parseCoordPair(step.toCoord || `${step.x2 || ""},${step.y2 || ""}`);
          const duration = Math.max(80, parseNum(step.durationMs, 350));
          if (!from || !to) throw new Error(`step_${i}_invalid_swipe_coord`);
          await runAdb(["shell", "input", "swipe", String(Math.round(from.x)), String(Math.round(from.y)), String(Math.round(to.x)), String(Math.round(to.y)), String(Math.round(duration))], 12000);
        } else if (action === "wait") {
          await sleep(Math.max(0, parseNum(step.ms, 1000)));
        } else if (action === "back") {
          await adbBack();
        } else if (action === "home") {
          await runAdb(["shell", "input", "keyevent", "3"], 8000);
        } else if (action === "exit_app_home") {
          const pkg = resolveTemplateText(step.packageName || workflow.packageName || "", vars).trim();
          if (pkg) {
            try {
              await runAdb(["shell", "am", "force-stop", pkg], 10000);
            } catch (_err) {
              // ignore force-stop error and still go home
            }
          }
          await runAdb(["shell", "input", "keyevent", "3"], 8000);
        } else if (action === "tap_text") {
          const words = resolveTemplateText(step.words || "", vars).split(/[|,]/).map((x) => x.trim()).filter(Boolean);
          if (!words.length) throw new Error(`step_${i}_missing_words`);
          const ok = await tapNodeByText(words, waitMs, Math.max(0, parseNum(step.retries, 1)));
          if (!ok) throw new Error(`step_${i}_tap_text_not_found`);
        } else if (action === "input_text") {
          const rawTpl = String(step.text || "");
          const text = resolveTemplateText(rawTpl, vars).trim();
          if (!text) throw new Error(`step_${i}_input_text_empty`);
          if (/\{[^}]+\}/.test(text)) {
            throw new Error(`step_${i}_template_unresolved:${text}`);
          }
          const hasNonAscii = /[^\x00-\x7F]/.test(text);
          const explicitCodeRequested = /\{(目的地代码|toCode)\}/.test(rawTpl);
          const candidates = [];
          if (!hasNonAscii) candidates.push(text);
          if (hasNonAscii) {
            const ascii = toAsciiCityFallback(text, vars, { allowCodeFallback: true });
            if (ascii && ascii !== text) candidates.push(ascii);
            if (vars.toCode) {
              candidates.push(String(vars.toCode).toLowerCase());
              candidates.push(String(vars.toCode).toUpperCase());
            }
            if (explicitCodeRequested && vars.toCode) candidates.push(String(vars.toCode));
          }

          let done = false;
          if (hasNonAscii) {
            try {
              done = await adbPasteText(text);
              pushDebug({ type: "input_text_paste", stepIndex: i, text, ok: !!done });
            } catch (_err) {
              pushDebug({ type: "input_text_paste", stepIndex: i, text, ok: false });
              // fallback below
            }
          }
          const tried = new Set();
          for (const one of (done ? [] : candidates)) {
            const candidate = String(one || "").trim();
            if (!candidate || tried.has(candidate)) continue;
            tried.add(candidate);
            try {
              await adbInputText(candidate);
              done = true;
              break;
            } catch (_err) {
              // continue
            }
          }
          if (!done) {
            if (hasNonAscii) {
              throw new Error(`step_${i}_input_text_paste_required:${text}`);
            } else {
              const pasted = await adbPasteText(text);
              if (!pasted) {
                throw new Error(`step_${i}_input_text_failed:${text}`);
              }
            }
          }
        } else if (action === "calendar_tap_dates") {
          const rawTpl = String(step.text || step.extra || "{出发日期}|{返程日期}");
          const resolved = resolveTemplateText(rawTpl, vars);
          const dates = [];
          const explicit = extractDatesFromText(resolved);
          if (explicit.length) {
            dates.push(...explicit);
          } else {
            const wantsDep = rawTpl.includes("{出发日期}") || (vars.depDate && resolved.includes(vars.depDate));
            const wantsRet = rawTpl.includes("{返程日期}") || (vars.retDate && resolved.includes(vars.retDate));
            if (wantsDep && vars.depDate) dates.push(vars.depDate);
            if (wantsRet && vars.retDate) dates.push(vars.retDate);
          }
          if (!dates.length) {
            if (vars.depDate) dates.push(vars.depDate);
            if (vars.retDate) dates.push(vars.retDate);
          }
          if (!dates.length) {
            throw new Error(`step_${i}_calendar_missing_dates`);
          }
          pushDebug({ type: "calendar_dates", stepIndex: i, rawTpl, resolved, dates });
          let firstBounds = null;
          for (let k = 0; k < dates.length; k += 1) {
            const b = await tapCalendarDate(dates[k], {
              retries: Math.max(1, parseNum(step.retries, 5)),
              waitAfter: Math.max(150, parseNum(step.waitMs, 650)),
              avoidBounds: k > 0 ? firstBounds : null,
              onDebug: (evt) => pushDebug({
                type: "calendar_debug",
                stepIndex: i,
                date: dates[k],
                ...evt
              })
            });
            pushDebug({ type: "calendar_tap_done", stepIndex: i, date: dates[k], bounds: b });
            if (!firstBounds) firstBounds = b;
          }
        } else if (action === "ocr_min_price") {
          const img = await captureCurrentMobileScreen("ocr");
          lastScreenPath = img;
          const region = parseRegionText(step.region || "");
          if (region) {
            pushDebug({
              type: "ocr_region",
              stepIndex: i,
              action,
              bounds: {
                x1: region.x1,
                y1: region.y1,
                x2: region.x2,
                y2: region.y2,
                cx: (region.x1 + region.x2) / 2,
                cy: (region.y1 + region.y2) / 2
              }
            });
          }
          const words = await ocrWords(img);
          const candidates = extractAmountsFromOcrWords(words, region);
          pushDebug({ type: "ocr_price_candidates", stepIndex: i, top: candidates.slice(0, 12) });
          if (candidates.length) {
            const vals = candidates.map((x) => x.amount);
            priceCandidates.push(...vals);
            pushDebug({ type: "ocr_price_pick", stepIndex: i, chosen: candidates[0] });
          } else {
            const txt = await ocrImageMaybeRegion(img, step.region || "");
            const rawVals = extractAmountsGeneric(txt);
            const best = extractPrice(txt, { url: "workflow_fallback_text" });
            const vals = best.ok && Number.isFinite(best.amount)
              ? [Number(best.amount), ...rawVals.filter((x) => x !== Number(best.amount))]
              : rawVals;
            if (vals.length) {
              let hints = extractNumericWordHints(words, region, vals);
              if (!hints.length) {
                try {
                  const nodes = await dumpUiNodes("ocr_ui_hint");
                  hints = extractPriceUiHints(nodes, region, vals);
                } catch (_err) {
                  // ignore UI hint failures
                }
              }
              const valsPreferred = vals.find((x) => Number.isFinite(Number(x)) && Number(x) >= candidateFloor);
              const chosenFromVals = Number.isFinite(Number(valsPreferred)) ? Number(valsPreferred) : Number(vals[0]);
              const hintTopAmount = hints.length ? Number(hints[0].amount) : null;
              let chosen = Number.isFinite(chosenFromVals) ? chosenFromVals : hintTopAmount;
              if (
                Number.isFinite(hintTopAmount)
                && Number.isFinite(chosenFromVals)
                && Math.abs(hintTopAmount - chosenFromVals) <= 5
              ) {
                // Same number from two sources, keep hint for better tap anchor consistency.
                chosen = hintTopAmount;
              }
              const allowFullscreenFallback = !region;
              if ((!Number.isFinite(chosen) || chosen < candidateFloor) && hasRoundTrip && allowFullscreenFallback) {
                const fullTxt = await ocrImage(img);
                const fullBest = extractPrice(fullTxt, { url: "workflow_fullscreen_fallback" });
                if (fullBest.ok && Number.isFinite(fullBest.amount) && fullBest.amount >= candidateFloor) {
                  chosen = Number(fullBest.amount);
                  pushDebug({ type: "ocr_price_fullscreen_fallback", stepIndex: i, amount: chosen });
                }
              } else if ((!Number.isFinite(chosen) || chosen < candidateFloor) && hasRoundTrip && region) {
                pushDebug({ type: "ocr_region_strict_skip", stepIndex: i, candidate: Number(chosen || 0) });
              }
              if (Number.isFinite(chosen)) priceCandidates.push(chosen);
              pushDebug({
                type: "ocr_price_text_fallback",
                stepIndex: i,
                values: vals.slice(0, 8),
                hints,
                chosen,
                chosenFromVals,
                hintTopAmount
              });
            }
          }
        } else if (action === "ocr_screen_price") {
          const img = await captureCurrentMobileScreen("ocr_screen");
          lastScreenPath = img;
          const region = parseRegionText(step.region || "");
          if (region) {
            pushDebug({
              type: "ocr_region",
              stepIndex: i,
              action,
              bounds: {
                x1: region.x1,
                y1: region.y1,
                x2: region.x2,
                y2: region.y2,
                cx: (region.x1 + region.x2) / 2,
                cy: (region.y1 + region.y2) / 2
              }
            });
          }
          const words = await ocrWords(img);
          const candidates = extractAmountsFromOcrWords(words, region);
          pushDebug({
            type: "ocr_price_candidates",
            stepIndex: i,
            top: candidates.slice(0, 12)
          });
          if (candidates.length) {
            const vals = candidates.map((x) => x.amount);
            priceCandidates.push(...vals);
            pushDebug({
              type: "ocr_price_pick",
              stepIndex: i,
              chosen: candidates[0]
            });
          } else {
            const txt = await ocrImageMaybeRegion(img, step.region || "");
            const rawVals = extractAmountsGeneric(txt);
            const best = extractPrice(txt, { url: "workflow_fallback_text" });
            const vals = best.ok && Number.isFinite(best.amount)
              ? [Number(best.amount), ...rawVals.filter((x) => x !== Number(best.amount))]
              : rawVals;
            if (vals.length) {
              let hints = extractNumericWordHints(words, region, vals);
              if (!hints.length) {
                try {
                  const nodes = await dumpUiNodes("ocr_ui_hint");
                  hints = extractPriceUiHints(nodes, region, vals);
                } catch (_err) {
                  // ignore UI hint failures
                }
              }
              const valsPreferred = vals.find((x) => Number.isFinite(Number(x)) && Number(x) >= candidateFloor);
              const chosenFromVals = Number.isFinite(Number(valsPreferred)) ? Number(valsPreferred) : Number(vals[0]);
              const hintTopAmount = hints.length ? Number(hints[0].amount) : null;
              let chosen = Number.isFinite(chosenFromVals) ? chosenFromVals : hintTopAmount;
              if (
                Number.isFinite(hintTopAmount)
                && Number.isFinite(chosenFromVals)
                && Math.abs(hintTopAmount - chosenFromVals) <= 5
              ) {
                chosen = hintTopAmount;
              }
              const allowFullscreenFallback = !region;
              if ((!Number.isFinite(chosen) || chosen < candidateFloor) && hasRoundTrip && allowFullscreenFallback) {
                const fullTxt = await ocrImage(img);
                const fullBest = extractPrice(fullTxt, { url: "workflow_fullscreen_fallback" });
                if (fullBest.ok && Number.isFinite(fullBest.amount) && fullBest.amount >= candidateFloor) {
                  chosen = Number(fullBest.amount);
                  pushDebug({ type: "ocr_price_fullscreen_fallback", stepIndex: i, amount: chosen });
                }
              } else if ((!Number.isFinite(chosen) || chosen < candidateFloor) && hasRoundTrip && region) {
                pushDebug({ type: "ocr_region_strict_skip", stepIndex: i, candidate: Number(chosen || 0) });
              }
              if (Number.isFinite(chosen)) priceCandidates.push(chosen);
              pushDebug({
                type: "ocr_price_text_fallback",
                stepIndex: i,
                values: vals.slice(0, 8),
                hints,
                chosen,
                chosenFromVals,
                hintTopAmount
              });
            }
          }
        } else if (action === "loop_swipe_ocr") {
          const loops = Math.max(1, parseNum(step.iterations, 3));
          const from = parseCoordPair(step.fromCoord || `${step.x1 || ""},${step.y1 || ""}`) || { x: 600, y: 1900 };
          const to = parseCoordPair(step.toCoord || `${step.x2 || ""},${step.y2 || ""}`) || { x: 600, y: 900 };
          const duration = Math.max(80, parseNum(step.durationMs, 400));
          for (let loop = 0; loop < loops; loop += 1) {
            const img = await captureCurrentMobileScreen("loop_ocr");
            lastScreenPath = img;
            const region = parseRegionText(step.region || "");
            if (region) {
              pushDebug({
                type: "ocr_region",
                stepIndex: i,
                action,
                loop,
                bounds: {
                  x1: region.x1,
                  y1: region.y1,
                  x2: region.x2,
                  y2: region.y2,
                  cx: (region.x1 + region.x2) / 2,
                  cy: (region.y1 + region.y2) / 2
                }
              });
            }
            const words = await ocrWords(img);
            const candidates = extractAmountsFromOcrWords(words, region);
            pushDebug({ type: "ocr_price_candidates", stepIndex: i, loop, top: candidates.slice(0, 12) });
            if (candidates.length) {
              const vals = candidates.map((x) => x.amount);
              priceCandidates.push(...vals);
              pushDebug({ type: "ocr_price_pick", stepIndex: i, loop, chosen: candidates[0] });
            } else {
              const txt = await ocrImageMaybeRegion(img, step.region || "");
              const rawVals = extractAmountsGeneric(txt);
              const best = extractPrice(txt, { url: "workflow_fallback_text" });
              const vals = best.ok && Number.isFinite(best.amount)
                ? [Number(best.amount), ...rawVals.filter((x) => x !== Number(best.amount))]
                : rawVals;
              if (vals.length) {
                let hints = extractNumericWordHints(words, region, vals);
                if (!hints.length) {
                  try {
                    const nodes = await dumpUiNodes("ocr_ui_hint");
                    hints = extractPriceUiHints(nodes, region, vals);
                  } catch (_err) {
                    // ignore UI hint failures
                  }
                }
                const valsPreferred = vals.find((x) => Number.isFinite(Number(x)) && Number(x) >= candidateFloor);
                const chosenFromVals = Number.isFinite(Number(valsPreferred)) ? Number(valsPreferred) : Number(vals[0]);
                const hintTopAmount = hints.length ? Number(hints[0].amount) : null;
                let chosen = Number.isFinite(chosenFromVals) ? chosenFromVals : hintTopAmount;
                if (
                  Number.isFinite(hintTopAmount)
                  && Number.isFinite(chosenFromVals)
                  && Math.abs(hintTopAmount - chosenFromVals) <= 5
                ) {
                  chosen = hintTopAmount;
                }
                const allowFullscreenFallback = !region;
                if ((!Number.isFinite(chosen) || chosen < candidateFloor) && hasRoundTrip && allowFullscreenFallback) {
                  const fullTxt = await ocrImage(img);
                  const fullBest = extractPrice(fullTxt, { url: "workflow_fullscreen_fallback" });
                  if (fullBest.ok && Number.isFinite(fullBest.amount) && fullBest.amount >= candidateFloor) {
                    chosen = Number(fullBest.amount);
                    pushDebug({ type: "ocr_price_fullscreen_fallback", stepIndex: i, loop, amount: chosen });
                  }
                } else if ((!Number.isFinite(chosen) || chosen < candidateFloor) && hasRoundTrip && region) {
                  pushDebug({ type: "ocr_region_strict_skip", stepIndex: i, loop, candidate: Number(chosen || 0) });
                }
                if (Number.isFinite(chosen)) priceCandidates.push(chosen);
                pushDebug({
                  type: "ocr_price_text_fallback",
                  stepIndex: i,
                  loop,
                  values: vals.slice(0, 8),
                  hints,
                  chosen,
                  chosenFromVals,
                  hintTopAmount
                });
              }
            }
            if (loop < loops - 1) {
              await runAdb(["shell", "input", "swipe", String(Math.round(from.x)), String(Math.round(from.y)), String(Math.round(to.x)), String(Math.round(to.y)), String(Math.round(duration))], 12000);
              await sleep(Math.max(200, waitMs));
            }
          }
        } else {
          throw new Error(`step_${i}_unsupported_action:${action}`);
        }

        if (waitMs > 0 && !["wait", "tap_text", "loop_swipe_ocr"].includes(action)) {
          await sleep(waitMs, abortSignal);
        }
      }
      pushDebug({ type: "step_done", stepIndex: i, action });
    }
  } catch (error) {
    if (isAbortError(error) || abortSignal?.aborted) {
      error = buildWorkflowAbortError();
    }
    pushDebug({ type: "workflow_error", error: String(error?.message || error) });
    let debugScreen = null;
    if (debugEnabled) {
      try {
        debugScreen = await captureScreenAsBase64("workflow_debug_error");
      } catch (_err) {
        debugScreen = null;
      }
      error.debug = { events: debugEvents, screen: debugScreen };
    }
    throw error;
  }

  let cleaned = priceCandidates.filter((x) => Number.isFinite(x) && x >= candidateFloor && x <= 20000);
  let finalScreen = null;
  if (postRunOcrEnabled) {
    let autoOcrAmounts = [];
    let autoBestAmount = null;
    try {
      const post = await captureScreenWithOcr("workflow_result");
      finalScreen = post?.screen || null;
      autoOcrAmounts = Array.isArray(post?.amounts)
        ? post.amounts.filter((x) => Number.isFinite(x) && x >= candidateFloor && x <= 20000)
        : [];
      autoBestAmount = Number.isFinite(Number(post?.bestAmount)) ? Number(post.bestAmount) : null;
      pushDebug({
        type: "post_run_ocr",
        amountCount: autoOcrAmounts.length,
        bestAmount: autoBestAmount,
        ocrTextLength: Number(post?.ocrTextLength || 0)
      });
    } catch (_err) {
      pushDebug({ type: "post_run_ocr_failed" });
      finalScreen = null;
      autoOcrAmounts = [];
      autoBestAmount = null;
    }
    if (!cleaned.length) {
      if (Number.isFinite(autoBestAmount)) {
        cleaned = [autoBestAmount];
      } else if (autoOcrAmounts.length) {
        cleaned = autoOcrAmounts;
      }
    }
  }
  const amount = cleaned.length ? Math.min(...cleaned) : null;
  let debugScreen = null;
  if (debugEnabled) {
    try {
      debugScreen = await captureScreenAsBase64("workflow_debug");
    } catch (_err) {
      debugScreen = null;
    }
  }
  return {
    ok: amount !== null,
    amount: amount === null ? undefined : amount,
    display: amount === null ? "" : `¥${amount.toLocaleString("zh-CN")}`,
    traces,
    scannedCount: cleaned.length,
    screenshot: lastScreenPath,
    finalScreen,
    debug: debugEnabled ? { events: debugEvents, screen: debugScreen } : undefined
  };
}

async function runSingleMobileAttempt(url, manualWaitMs, forcedTarget) {
  let localImagePath = "";
  let preprocessedPath = "";
  let targetLabel = "unknown";
  try {
    const first = await captureMobilePage(url, forcedTarget);
    localImagePath = first.localPath;
    targetLabel = first.target?.label || "unknown";
    preprocessedPath = await preprocessImageForOcr(localImagePath);
    let text = `${await ocrImage(localImagePath)}\n${await ocrImage(preprocessedPath)}`;

    if (/(验证|captcha|robot|请稍后|网络异常)/i.test(text) && manualWaitMs > 0) {
      await sleep(manualWaitMs);
      const second = await captureMobilePage(url);
      localImagePath = second.localPath;
      targetLabel = second.target?.label || targetLabel;
      preprocessedPath = await preprocessImageForOcr(localImagePath);
      text = `${await ocrImage(localImagePath)}\n${await ocrImage(preprocessedPath)}`;
    }

    let result = extractPrice(text, { url });
    if (!result.ok) {
      const s = await getScreenSize();
      await runAdb(
        ["shell", "input", "swipe", String(Math.floor(s.width * 0.5)), String(Math.floor(s.height * 0.78)), String(Math.floor(s.width * 0.5)), String(Math.floor(s.height * 0.46)), "220"],
        10000
      );
      await sleep(900);
      const third = await captureMobilePage(url, forcedTarget);
      localImagePath = third.localPath;
      preprocessedPath = await preprocessImageForOcr(localImagePath);
      text = `${await ocrImage(localImagePath)}\n${await ocrImage(preprocessedPath)}`;
      result = extractPrice(text, { url });
    }
    if (result.ok) {
      return { ...result, source: "mobile_ocr", channel: targetLabel };
    }

    return {
      ok: false,
      reason: result.reason || "price_not_found",
      channel: targetLabel,
      snippet: normalizeText(text).slice(0, 220)
    };
  } catch (error) {
    return {
      ok: false,
      reason: "mobile_scrape_failed",
      channel: targetLabel,
      error: String(error?.message || error)
    };
  } finally {
    if (localImagePath) {
      try {
        await fs.unlink(localImagePath);
      } catch (_err) {
        // ignore local cleanup error
      }
    }
    if (preprocessedPath) {
      try {
        await fs.unlink(preprocessedPath);
      } catch (_err) {
        // ignore local cleanup error
      }
    }
  }
}

async function scrapePriceFromMobile(url, manualWaitMs = DEFAULT_MANUAL_CHALLENGE_WAIT_MS) {
  const primary = inferMobileTarget(url);
  const attempts = [];
  for (let i = 0; i < 2; i += 1) {
    const one = await runSingleMobileAttempt(url, manualWaitMs, primary);
    attempts.push(one);
    if (one.ok) return one;
  }

  const fallback = buildFallbackTarget(primary);
  if (fallback && (primary.channel !== "app" || MOBILE_APP_FALLBACK_TO_BROWSER)) {
    for (let i = 0; i < 2; i += 1) {
      const one = await runSingleMobileAttempt(url, Math.min(8000, manualWaitMs), fallback);
      attempts.push(one);
      if (one.ok) return one;
    }
  }

  const bestFailure = attempts.find((x) => x.reason === "no_result")
    || attempts.find((x) => x.reason === "captcha_required")
    || attempts[attempts.length - 1]
    || { ok: false, reason: "mobile_scrape_failed", channel: primary.label };
  return bestFailure;
}

async function initBrowserContext() {
  const profileDir = path.resolve(process.cwd(), ".price-agent-profile");
  let context;
  let browser;
  let runMode = "local_profile";

  if (!FORCE_LOCAL_PROFILE) {
    try {
      const cdpBrowser = await chromium.connectOverCDP(CDP_URL);
      browser = cdpBrowser;
      const existing = cdpBrowser.contexts();
      context = existing[0] || await cdpBrowser.newContext();
      runMode = "cdp_user_browser";
      process.stdout.write(`price-agent attached to user browser via CDP: ${CDP_URL}\n`);
    } catch (_err) {
      runMode = "local_profile";
    }
  }

  if (!context && REQUIRE_CDP) {
    throw new Error(`cdp_unavailable: cannot connect to ${CDP_URL}. Please start your existing Chrome with remote debugging port.`);
  }

  if (!context) {
    try {
      context = await chromium.launchPersistentContext(profileDir, {
        headless: false,
        locale: "zh-CN",
        timezoneId: "Asia/Shanghai",
        userAgent:
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        args: ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
      });
      runMode = "local_profile";
    } catch (_err) {
      const transientBrowser = await chromium.launch({
        headless: false,
        args: ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
      });
      browser = transientBrowser;
      context = await transientBrowser.newContext({
        locale: "zh-CN",
        timezoneId: "Asia/Shanghai",
        userAgent:
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
      });
      runMode = "transient_context";
    }
  }

  return { context, runMode, browser };
}

async function start() {
  const app = express();
  app.use(express.json({ limit: "1mb" }));
  app.use((req, res, next) => {
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type");
    if (req.method === "OPTIONS") {
      return res.status(204).end();
    }
    next();
  });

  let browserContext = null;
  let browserInstance = null;
  let runMode = "unknown";
  let mobileReady = false;
  let mobileLastError = "";

  const ensureMobileReadySafe = async () => {
    try {
      await ensureMobileReady();
      mobileReady = true;
      mobileLastError = "";
      return true;
    } catch (err) {
      mobileReady = false;
      mobileLastError = String(err?.message || err);
      return false;
    }
  };

  if (SOURCE === "mobile") {
    await ensureMobileReadySafe();
    runMode = mobileReady ? "adb_mobile" : "adb_mobile_no_device";
  } else {
    const browserInit = await initBrowserContext();
    browserContext = browserInit.context;
    browserInstance = browserInit.browser || null;
    runMode = browserInit.runMode;
  }

  const ensureBrowserContext = () => {
    if (!browserContext) {
      throw new Error("browser_unavailable");
    }
    return browserContext;
  };
  const getBrowserPages = () => {
    if (browserInstance && typeof browserInstance.pages === "function") {
      return browserInstance.pages();
    }
    if (browserContext && typeof browserContext.pages === "function") {
      return browserContext.pages();
    }
    return [];
  };

  app.get("/health", (_req, res) => {
    res.json({
      ok: true,
      service: "price-agent",
      port: PORT,
      source: SOURCE,
      mode: runMode,
      cdp: CDP_URL,
      mobileReady: SOURCE === "mobile" ? mobileReady : undefined,
      mobileLastError: SOURCE === "mobile" ? (mobileLastError || undefined) : undefined,
      adbPath: SOURCE === "mobile" ? ADB_PATH : undefined,
      adbDevice: SOURCE === "mobile" ? (ADB_DEVICE || "auto") : undefined
    });
  });

  app.post("/api/live-prices", async (req, res) => {
    try {
      const items = Array.isArray(req.body?.items) ? req.body.items.slice(0, MAX_ITEMS) : [];
      const manualWaitMsRaw = Number(req.body?.manualWaitMs);
      const manualWaitMs = Number.isFinite(manualWaitMsRaw)
        ? Math.max(0, Math.min(180000, manualWaitMsRaw))
        : DEFAULT_MANUAL_CHALLENGE_WAIT_MS;
      if (!items.length) {
        return res.status(400).json({ ok: false, error: "items_required" });
      }

      let results;
      if (SOURCE === "mobile") {
        results = [];
        for (const item of items) {
          const key = String(item?.key || "");
          const url = String(item?.url || "");
          if (!key || !url) {
            results.push({ key, ok: false, reason: "invalid_item" });
            continue;
          }
          const parsed = await scrapePriceFromMobile(url, manualWaitMs);
          results.push({ key, ...parsed });
        }
      } else {
        results = await Promise.all(items.map(async (item) => {
          const key = String(item?.key || "");
          const url = String(item?.url || "");
          if (!key || !url) {
            return { key, ok: false, reason: "invalid_item" };
          }

          try {
            const parsed = await scrapePriceFromBrowser(browserContext, url, manualWaitMs);
            return { key, ...parsed };
          } catch (error) {
            return { key, ok: false, reason: "scrape_failed", error: String(error?.message || error) };
          }
        }));
      }

      return res.json({ ok: true, updatedAt: Date.now(), results });
    } catch (error) {
      return res.status(500).json({ ok: false, error: String(error?.message || error) });
    }
  });

  app.get("/api/browser/pages", async (_req, res) => {
    try {
      ensureBrowserContext();
      const cdpPages = await fetchCdpTargets();
      if (cdpPages.length) {
        const list = cdpPages.map((p, idx) => ({
          index: idx,
          title: String(p.title || ""),
          url: String(p.url || ""),
          ws: String(p.webSocketDebuggerUrl || "")
        }));
        return res.json({ ok: true, pages: list, source: "cdp" });
      }
      const pages = getBrowserPages();
      const list = await Promise.all(pages.map(async (p, idx) => {
        let title = "";
        let url = "";
        try { title = await p.title(); } catch (_err) {}
        try { url = String(p.url() || ""); } catch (_err) {}
        return { index: idx, title, url };
      }));
      return res.json({ ok: true, pages: list, source: "playwright" });
    } catch (error) {
      const msg = String(error?.message || error);
      const status = msg.includes("browser_unavailable") ? 503 : 500;
      return res.status(status).json({ ok: false, error: msg });
    }
  });

  app.post("/api/browser/ocr/flight", async (req, res) => {
    try {
      ensureBrowserContext();
      const pageIndex = Number(req.body?.pageIndex);
      const planId = String(req.body?.planId || "").trim();
      if (!Number.isFinite(pageIndex) || pageIndex < 0) {
        return res.status(400).json({ ok: false, error: "page_index_required" });
      }
      if (!planId) return res.status(400).json({ ok: false, error: "plan_id_required" });
      const planName = String(req.body?.planName || "");
      const depDate = String(req.body?.depDate || "");
      const retDate = String(req.body?.retDate || "");
      const tripType = String(req.body?.tripType || "round");
      const direction = String(req.body?.direction || (tripType === "oneway" ? "outbound" : "round"));
      const persist = req.body?.persist !== false;

      let title = "";
      let url = "";
      let domText = "";
      let extractedByCode = [];
      const cdpPages = await fetchCdpTargets();
      if (cdpPages.length) {
        const target = cdpPages[pageIndex];
        if (!target) return res.status(404).json({ ok: false, error: "page_not_found" });
        title = String(target.title || "");
        url = String(target.url || "");
        const wsUrl = String(target.webSocketDebuggerUrl || "");
        if (!wsUrl) return res.status(500).json({ ok: false, error: "page_ws_unavailable" });
        try {
          const hintedPlatform = detectPlatformFromUrl(url) || detectPlatformFromText(url, title, "");
          const extracted = await cdpEvaluate(wsUrl, buildFlightCodeExtractExpression(hintedPlatform), 20000);
          extractedByCode = Array.isArray(extracted?.items) ? extracted.items : [];
        } catch (_err) {
          extractedByCode = [];
        }
        if (!extractedByCode.length) {
          try {
            domText = String(await cdpEvaluate(wsUrl, "document && document.body ? document.body.innerText : ''", 15000) || "");
          } catch (_err) {
            domText = "";
          }
        }
      } else {
        const pages = getBrowserPages();
        const page = pages[pageIndex];
        if (!page) return res.status(404).json({ ok: false, error: "page_not_found" });
        title = await page.title();
        url = String(page.url() || "");
        try {
          extractedByCode = await page.evaluate(() => {
            const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
            const normalizeTime = (s) => {
              const m = clean(s).match(/([01]?\d|2[0-3])[:：]([0-5]\d)/);
              if (!m) return "";
              return String(m[1]).padStart(2, "0") + ":" + String(m[2]).padStart(2, "0");
            };
            const extractTimes = (s) => {
              const out = [];
              const reg = /([01]?\d|2[0-3])[:：]([0-5]\d)/g;
              let m;
              const txt = String(s || "");
              while ((m = reg.exec(txt))) {
                out.push(String(m[1]).padStart(2, "0") + ":" + String(m[2]).padStart(2, "0"));
              }
              return out;
            };
            const parsePrice = (s) => {
              const txt = clean(s);
              if (!txt) return null;
              const digits = txt.replace(/[^0-9]/g, "");
              if (!digits) return null;
              const n = Number(digits);
              if (!Number.isFinite(n) || n < 300 || n > 5000) return null;
              return n;
            };
            const rowSelectors = [
              ".b-airfly",
              ".flight-item",
              ".flight-list-item",
              ".result-item",
              ".ticket-item",
              "li[class*='flight']",
              "div[class*='flight-item']",
              "div[class*='flt-item']",
              "li[class*='flt-item']",
              "[data-testid*='flight']",
              "[class*='flightRow']",
              "[class*='flight-row']"
            ];
            const rows = [];
            const rowSeen = new Set();
            rowSelectors.forEach((sel) => {
              document.querySelectorAll(sel).forEach((row) => {
                if (!row || rowSeen.has(row)) return;
                rowSeen.add(row);
                rows.push(row);
              });
            });
            return rows.map((row) => {
              const rowText = clean(row?.innerText || "");
              if (!rowText) return null;
              if (rowText.length > 700) return null;
              const dep = normalizeTime(row.querySelector(".sep-lf, .from .time, .depart .time, .depart-time, .time-depart")?.textContent || "");
              const arr = normalizeTime(row.querySelector(".sep-rt, .to .time, .arrive .time, .arrive-time, .time-arrive")?.textContent || "");
              const times = extractTimes(rowText);
              const depTime = dep || times[0] || "";
              const arrTime = arr || times[1] || "";
              if (!depTime || !arrTime) return null;
              const extractRowPrice = (root, txt) => {
                const selectors = [
                  ".col-price .prc .fix_price",
                  ".fix_price",
                  ".price .num",
                  ".ticket-price",
                  ".col-price .prc",
                  "[data-role='price']",
                  "[class*='price'][title]",
                  "[class*='price']",
                  "[class*='prc']"
                ];
                const nodes = [];
                const seen = new Set();
                for (const sel of selectors) {
                  root.querySelectorAll(sel).forEach((n) => {
                    if (!n || seen.has(n)) return;
                    seen.add(n);
                    nodes.push(n);
                  });
                }
                const prices = [];
                nodes.forEach((n) => {
                  const p1 = parsePrice(n?.getAttribute?.("title") || "");
                  const p2 = parsePrice(n?.textContent || "");
                  if (p1) prices.push(p1);
                  if (p2) prices.push(p2);
                });
                const fallbackToken = (txt.match(/[¥￥]\s*[0-9][0-9,]*/) || [])[0] || "";
                const fallbackRise = (txt.match(/([3-9]\d{2}|[1-4]\d{3})\s*起/) || [])[1] || "";
                const p3 = parsePrice(fallbackToken);
                const p4 = parsePrice(fallbackRise);
                if (p3) prices.push(p3);
                if (p4) prices.push(p4);
                if (!prices.length) return null;
                return Math.min(...prices);
              };
              const amount = extractRowPrice(row, rowText);
              if (!amount) return null;
              return {
                depTime,
                arrTime,
                amount,
                airline: clean(row.querySelector(".air, .airline-name, .company-name")?.textContent || ""),
                flightNo: clean(row.querySelector(".num .n, .flightNo, .flight-no")?.textContent || ""),
                depAirport: clean(row.querySelectorAll(".airport, .airport-name, .station, .terminal")?.[0]?.textContent || ""),
                arrAirport: clean(row.querySelectorAll(".airport, .airport-name, .station, .terminal")?.[1]?.textContent || "")
              };
            }).filter(Boolean);
          });
        } catch (_err) {
          extractedByCode = [];
        }
        if (!extractedByCode.length) {
          try {
            domText = String(await page.evaluate(() => (document && document.body ? document.body.innerText : "")) || "");
          } catch (_err) {
            domText = "";
          }
        }
      }
      const platformCode = detectPlatformFromText(url, title, domText) || detectPlatformFromUrl(url);
      if (!platformCode) {
        return res.status(400).json({ ok: false, error: "platform_unknown" });
      }

      const items = normalizeExtractedFlightItems(extractedByCode);
      const fallbackItems = items.length ? [] : parseFlightItemsFromDomText(domText);
      const finalItems = items.length ? items : fallbackItems;
      const parsedBy = items.length ? "dom_selector" : "dom_text";
      if (!finalItems.length) {
        return res.status(200).json({
          ok: true,
          previewId: "",
          items: [],
          platform: platformCode,
          tripType,
          direction,
          parsedBy: "dom_code",
          error: "no_flights_found_in_page_code"
        });
      }
      const now = Date.now();
      if (!persist) {
        const previewId = putOcrPreview({
          mode: "flight",
          planId,
          planName,
          depDate,
          retDate,
          tripType,
          direction,
          platformCode,
          items: finalItems,
          parsedBy
        });
        return res.json({ ok: true, previewId, items: finalItems, platform: platformCode, tripType, direction, parsedBy });
      }

      let saved = 0;
      for (const item of finalItems) {
        if (!item || !item.amount || !item.depTime || !item.arrTime) continue;
        await appendPriceHistory({
          planId,
          planName,
          platform: platformCode,
          amount: Number(item.amount),
          category: "flight",
          tripType,
          direction,
          depTime: item.depTime,
          arrTime: item.arrTime,
          airline: item.airline || "",
          flightNo: item.flightNo || "",
          depAirport: item.depAirport || "",
          arrAirport: item.arrAirport || "",
          source: "browser_code",
          createdAt: now
        });
        const refKey = `${tripType}_${direction}_${item.depTime}_${item.arrTime}`;
        saved += 1;
      }
      return res.json({ ok: true, saved, total: finalItems.length, platform: platformCode, tripType, direction, parsedBy });
    } catch (error) {
      const msg = String(error?.message || error);
      const status = msg.includes("browser_unavailable") ? 503 : 500;
      return res.status(status).json({ ok: false, error: msg });
    }
  });

  app.post("/api/browser/ocr/hotel", async (req, res) => {
    try {
      ensureBrowserContext();
      const pageIndex = Number(req.body?.pageIndex);
      const planId = String(req.body?.planId || "").trim();
      if (!Number.isFinite(pageIndex) || pageIndex < 0) {
        return res.status(400).json({ ok: false, error: "page_index_required" });
      }
      if (!planId) return res.status(400).json({ ok: false, error: "plan_id_required" });
      const planName = String(req.body?.planName || "");
      const nights = Array.isArray(req.body?.nights) ? req.body.nights : [];
      const persist = req.body?.persist !== false;
      let title = "";
      let url = "";
      let shot;
      const cdpPages = await fetchCdpTargets();
      if (cdpPages.length) {
        const target = cdpPages[pageIndex];
        if (!target) return res.status(404).json({ ok: false, error: "page_not_found" });
        title = String(target.title || "");
        url = String(target.url || "");
        const wsUrl = String(target.webSocketDebuggerUrl || "");
        if (!wsUrl) return res.status(500).json({ ok: false, error: "page_ws_unavailable" });
        const cap = await cdpCaptureScreenshot(wsUrl);
        const meta = await sharp(cap.buffer).metadata();
        const fileName = `browser_hotel_${Date.now()}_${Math.floor(Math.random() * 10000)}.png`;
        const localPath = path.join(MOBILE_TMP_DIR, fileName);
        await fs.mkdir(MOBILE_TMP_DIR, { recursive: true });
        await fs.writeFile(localPath, cap.buffer);
        shot = { localPath, buffer: cap.buffer, width: Number(meta.width || 0), height: Number(meta.height || 0) };
      } else {
        const pages = getBrowserPages();
        const page = pages[pageIndex];
        if (!page) return res.status(404).json({ ok: false, error: "page_not_found" });
        title = await page.title();
        url = String(page.url() || "");
        shot = await captureBrowserScreenshot(page, "browser_hotel");
      }
      const ocrText = await ocrTextFromImagePath(shot.localPath);
      const platformCode = detectPlatformFromText(url, title, ocrText) || detectPlatformFromUrl(url);
      if (!platformCode) {
        return res.status(400).json({ ok: false, error: "platform_unknown" });
      }

      const info = parseHotelInfoFromText(ocrText, title, nights);
      const now = Date.now();
      if (!persist) {
        const previewId = putOcrPreview({
          mode: "hotel",
          planId,
          planName,
          platformCode,
          info,
          shot
        });
        return res.json({ ok: true, previewId, info, platform: platformCode });
      }

      let saved = 0;
      if (info.amount && info.checkinDate) {
        await appendPriceHistory({
          planId,
          planName,
          platform: platformCode,
          amount: Number(info.amount),
          category: "hotel",
          hotelName: info.hotelName || "",
          checkinDate: info.checkinDate,
          source: "browser_ocr",
          createdAt: now
        });
        const refKey = `${info.checkinDate}__${info.hotelName || ""}`;
        await savePriceShot({
          planId,
          planName,
          platform: platformCode,
          displayPlatform: platformCodeToLabel(platformCode),
          category: "hotel",
          refKey,
          image_base64: shot.buffer.toString("base64"),
          width: shot.width,
          height: shot.height,
          updatedAt: now
        });
        saved = 1;
      }

      try { await fs.unlink(shot.localPath); } catch (_err) {}
      return res.json({ ok: true, saved, platform: platformCode });
    } catch (error) {
      const msg = String(error?.message || error);
      const status = msg.includes("browser_unavailable") ? 503 : 500;
      return res.status(status).json({ ok: false, error: msg });
    }
  });

  app.post("/api/browser/ocr/flight/commit", async (req, res) => {
    try {
      const previewId = String(req.body?.previewId || "").trim();
      if (!previewId) return res.status(400).json({ ok: false, error: "preview_id_required" });
      const data = getOcrPreview(previewId);
      if (!data || data.mode !== "flight") return res.status(404).json({ ok: false, error: "preview_not_found" });
      const { planId, planName, depDate, retDate, tripType, direction, items, shot } = data;
      const platformCode = normalizePlatformCode(data.platformCode || data.platform);
      if (!platformCode) {
        return res.status(400).json({ ok: false, error: "preview_platform_unknown" });
      }
      let saved = 0;
      let shotSaved = 0;
      let shotSkipped = 0;
      const now = Date.now();
      for (const item of items || []) {
        if (!item || !item.amount || !item.depTime || !item.arrTime) continue;
        await appendPriceHistory({
          planId,
          planName,
          platform: platformCode,
          amount: Number(item.amount),
          category: "flight",
          tripType,
          direction,
          depTime: item.depTime,
          arrTime: item.arrTime,
          airline: item.airline || "",
          source: "browser_ocr",
          createdAt: now
        });
        const refKey = `${tripType}_${direction}_${item.depTime}_${item.arrTime}`;
        if (shot?.buffer) {
          await savePriceShot({
            planId,
            planName,
            platform: platformCode,
            displayPlatform: platformCodeToLabel(platformCode),
            category: "flight",
            refKey,
            depDate,
            retDate,
            image_base64: shot.buffer.toString("base64"),
            width: Number(shot?.width || 0),
            height: Number(shot?.height || 0),
            updatedAt: now
          });
          shotSaved += 1;
        } else {
          shotSkipped += 1;
        }
        saved += 1;
      }
      OCR_PREVIEW_CACHE.delete(previewId);
      try { if (shot?.localPath) await fs.unlink(shot.localPath); } catch (_err) {}
      return res.json({ ok: true, saved, shotSaved, shotSkipped });
    } catch (error) {
      const msg = String(error?.message || error);
      process.stderr.write(`[browser/ocr/flight/commit] ${msg}\n`);
      return res.status(500).json({ ok: false, error: msg });
    }
  });

  app.post("/api/browser/ocr/hotel/commit", async (req, res) => {
    try {
      const previewId = String(req.body?.previewId || "").trim();
      if (!previewId) return res.status(400).json({ ok: false, error: "preview_id_required" });
      const data = getOcrPreview(previewId);
      if (!data || data.mode !== "hotel") return res.status(404).json({ ok: false, error: "preview_not_found" });
      const { planId, planName, platformCode, info, shot } = data;
      let saved = 0;
      const now = Date.now();
      if (info?.amount && info?.checkinDate) {
        await appendPriceHistory({
          planId,
          planName,
          platform: platformCode,
          amount: Number(info.amount),
          category: "hotel",
          hotelName: info.hotelName || "",
          checkinDate: info.checkinDate,
          source: "browser_ocr",
          createdAt: now
        });
        const refKey = `${info.checkinDate}__${info.hotelName || ""}`;
        await savePriceShot({
          planId,
          planName,
          platform: platformCode,
          displayPlatform: platformCodeToLabel(platformCode),
          category: "hotel",
          refKey,
          image_base64: shot?.buffer ? shot.buffer.toString("base64") : "",
          width: Number(shot?.width || 0),
          height: Number(shot?.height || 0),
          updatedAt: now
        });
        saved = 1;
      }
      OCR_PREVIEW_CACHE.delete(previewId);
      try { if (shot?.localPath) await fs.unlink(shot.localPath); } catch (_err) {}
      return res.json({ ok: true, saved });
    } catch (error) {
      return res.status(500).json({ ok: false, error: String(error?.message || error) });
    }
  });

  app.get("/api/workflows", async (_req, res) => {
    try {
      const data = await loadWorkflows();
      return res.json({ ok: true, ...data });
    } catch (error) {
      return res.status(500).json({ ok: false, error: String(error?.message || error) });
    }
  });

  app.get("/api/vlm/current_screen", async (_req, res) => {
    try {
      if (SOURCE !== "mobile") {
        return res.status(400).json({ success: false, msg: "only_mobile_source_supported" });
      }
      const ok = await ensureMobileReadySafe();
      if (!ok) {
        return res.status(503).json({ success: false, msg: mobileLastError || "no_mobile_device" });
      }
      const imgPath = await captureCurrentMobileScreen("picker");
      const buf = await fs.readFile(imgPath);
      const meta = await sharp(buf).metadata();
      try {
        await fs.unlink(imgPath);
      } catch (_err) {
        // ignore
      }
      return res.json({
        success: true,
        image_base64: buf.toString("base64"),
        width: Number(meta.width || 0),
        height: Number(meta.height || 0)
      });
    } catch (error) {
      return res.status(500).json({ success: false, msg: String(error?.message || error) });
    }
  });

  app.post("/api/workflows", async (req, res) => {
    try {
      const workflows = Array.isArray(req.body?.workflows) ? req.body.workflows : null;
      if (!workflows) return res.status(400).json({ ok: false, error: "workflows_required" });
      const saved = await saveWorkflows(workflows);
      return res.json({ ok: true, ...saved });
    } catch (error) {
      return res.status(500).json({ ok: false, error: String(error?.message || error) });
    }
  });

  app.get("/api/price-history", async (req, res) => {
    try {
      const planId = String(req.query?.planId || "").trim();
      const payload = await readJsonFileSafe(PRICE_HISTORY_FILE, { records: [] });
      const records = Array.isArray(payload.records) ? payload.records : [];
      const normalized = records.map((r) => normalizeHistoryRecord(r)).filter(Boolean);
      const list = planId ? normalized.filter((r) => r && r.planId === planId) : normalized;
      return res.json({ ok: true, records: list.slice(0, 120), updatedAt: payload.updatedAt || null });
    } catch (error) {
      return res.status(500).json({ ok: false, error: String(error?.message || error) });
    }
  });

  app.post("/api/price-history/append", async (req, res) => {
    try {
      const entry = req.body?.entry && typeof req.body.entry === "object" ? req.body.entry : null;
      if (!entry) return res.status(400).json({ ok: false, error: "entry_required" });
      const payload = await appendPriceHistory(entry);
      return res.json({ ok: true, updatedAt: payload.updatedAt });
    } catch (error) {
      return res.status(500).json({ ok: false, error: String(error?.message || error) });
    }
  });

  app.get("/api/price-shot/list", async (req, res) => {
    try {
      const planId = String(req.query?.planId || "").trim();
      const data = await listPriceShots(planId);
      return res.json({ ok: true, ...data });
    } catch (error) {
      return res.status(500).json({ ok: false, error: String(error?.message || error) });
    }
  });

  app.post("/api/price-shot/put", async (req, res) => {
    try {
      const shot = req.body?.shot && typeof req.body.shot === "object" ? req.body.shot : null;
      if (!shot) return res.status(400).json({ ok: false, error: "shot_required" });
      const payload = await savePriceShot(shot);
      return res.json({ ok: true, updatedAt: payload.updatedAt });
    } catch (error) {
      return res.status(500).json({ ok: false, error: String(error?.message || error) });
    }
  });

  app.post("/api/price-history/delete", async (req, res) => {
    try {
      const id = String(req.body?.id || "").trim();
      if (!id) return res.status(400).json({ ok: false, error: "id_required" });
      const { payload, removed } = await deletePriceHistoryById(id);
      return res.json({ ok: true, removed, updatedAt: payload.updatedAt || Date.now() });
    } catch (error) {
      return res.status(500).json({ ok: false, error: String(error?.message || error) });
    }
  });

  app.post("/api/workflow/stop", async (_req, res) => {
    try {
      const hadRunning = !!ACTIVE_WORKFLOW_RUN;
      const stopped = stopActiveWorkflow("manual_stop");
      return res.json({
        ok: true,
        hadRunning,
        stopped,
        runId: ACTIVE_WORKFLOW_RUN?.id || null
      });
    } catch (error) {
      return res.status(500).json({ ok: false, error: String(error?.message || error) });
    }
  });

  app.post("/api/workflow/run", async (req, res) => {
    let runId = "";
    try {
      if (SOURCE !== "mobile") {
        return res.status(400).json({ ok: false, error: "workflow_run_requires_mobile_source" });
      }
      const ok = await ensureMobileReadySafe();
      if (!ok) {
        return res.status(503).json({ ok: false, error: mobileLastError || "no_mobile_device" });
      }
      if (ACTIVE_WORKFLOW_RUN && !ACTIVE_WORKFLOW_RUN.controller.signal.aborted) {
        return res.status(409).json({
          ok: false,
          error: "workflow_already_running",
          runId: ACTIVE_WORKFLOW_RUN.id
        });
      }
      const wfId = String(req.body?.workflowId || "").trim();
      const context = req.body?.context && typeof req.body.context === "object" ? req.body.context : {};
      if (((!context.toCity && !context.destination && !context["目的地"]) || !context.toCode) && context.url) {
        const trip = parseTripFromUrl(String(context.url || ""));
        if (trip.toCityName) context.toCity = trip.toCityName;
        if (trip.toCode) context.toCode = trip.toCode;
      }
      let workflow = req.body?.workflow;
      const debugRequested = !!req.body?.debug;
      const postRunOcr = req.body?.postRunOcr !== false;
      const persistHistory = req.body?.persistHistory !== false;
      if (!workflow && wfId) {
        const data = await loadWorkflows();
        workflow = (data.workflows || []).find((x) => x && String(x.id) === wfId);
      }
      if (!workflow) return res.status(404).json({ ok: false, error: "workflow_not_found" });

      const controller = new AbortController();
      runId = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
      ACTIVE_WORKFLOW_RUN = {
        id: runId,
        startedAt: Date.now(),
        workflowId: String(workflow.id || wfId || ""),
        workflowName: String(workflow.name || ""),
        controller,
        stopReason: ""
      };

      const result = await runVlmWorkflow(workflow, context, {
        debug: debugRequested,
        postRunOcr,
        signal: controller.signal
      });
      if (persistHistory && result.ok && Number.isFinite(result.amount) && Number(result.amount) > 0) {
        await appendPriceHistory({
          planId: String(context.planId || ""),
          planName: String(context.planName || ""),
          platform: String(context.platform || workflow.id || ""),
          routeLabel: String(context.routeLabel || ""),
          depDate: String(context.depDate || ""),
          retDate: String(context.retDate || ""),
          amount: result.amount,
          category: String(context.category || "flight"),
          tripType: String(context.tripType || "round"),
          direction: String(context.direction || "round"),
          depTime: String(context.depTime || ""),
          arrTime: String(context.arrTime || ""),
          flightNo: String(context.flightNo || ""),
          workflowId: String(workflow.id || ""),
          workflowName: String(workflow.name || "")
        });
      }
      return res.json({ ok: true, result, workflowId: String(workflow.id || "") });
    } catch (error) {
      if (isAbortError(error)) {
        return res.status(409).json({
          ok: false,
          error: "workflow_aborted",
          debug: error?.debug || null
        });
      }
      return res.status(500).json({
        ok: false,
        error: String(error?.message || error),
        debug: error?.debug || null
      });
    } finally {
      if (runId && ACTIVE_WORKFLOW_RUN && ACTIVE_WORKFLOW_RUN.id === runId) {
        ACTIVE_WORKFLOW_RUN = null;
      }
    }
  });

  const server = app.listen(PORT, () => {
    process.stdout.write(`price-agent listening on http://127.0.0.1:${PORT}\n`);
    process.stdout.write(`price-agent source: ${SOURCE}\n`);
    process.stdout.write(`price-agent mode: ${runMode}\n`);
  });

  async function shutdown() {
    try {
      if (ocrWorkerPromise) {
        const worker = await ocrWorkerPromise;
        await worker.terminate();
      }
    } catch (_err) {
      // ignore
    }
    server.close();
    process.exit(0);
  }

  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

start().catch((err) => {
  process.stderr.write(`failed to start price-agent: ${String(err?.message || err)}\n`);
  process.exit(1);
});
