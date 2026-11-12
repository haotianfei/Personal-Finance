/**
 * 应用版本唯一来源（前端）。
 * 发版时同步：frontend/package.json、backend/pyproject.toml、
 * backend/main.py (FastAPI version)、CHANGELOG.md、README.md / README_EN.md 徽章。
 */
export const APP_VERSION = process.env.NEXT_PUBLIC_APP_VERSION ?? '0.0.0'
