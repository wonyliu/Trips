# Mobile Workflow Tool (Copied from Taobao project)

This folder is copied from:

- `E:\Works\电商数据\淘宝价格监控系统`

Purpose:

- Use an existing visual workflow editor to orchestrate mobile operations step by step.
- Supports manual configuration for tap/swipe/coordinate/OCR style actions.

## Run

1. Open PowerShell in this folder:

```powershell
cd D:\JointProjects\Trips\mobile-workflow-tool
```

2. Install Python dependencies (first time only):

```powershell
pip install flask pandas apscheduler requests openpyxl pillow opencv-python
```

3. Start service:

```powershell
python app.py
```

4. Open in browser:

- Main visual workflow page: `http://127.0.0.1:5000/`
- Legacy page: `http://127.0.0.1:5000/legacy_api`

## Notes

- `config.json` and `.env` are copied with this tool.
- `records/`, `templates/`, `pic/` are also copied.
- If ADB is not in PATH, ensure your Android SDK `platform-tools` is installed and available.
- This is currently a standalone tool inside `Trips`; no automatic integration to `index.html` yet.
