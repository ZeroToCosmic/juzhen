# 页面清单

| 页面 | URL | Template/来源 | JS/CSS | 主要API | 测试 |
|---|---|---|---|---|---|
| 总览 | `/` | `gateway/app.py`/Dashboard模板 | shell/navigation | status、旧业务 | dashboard navigation |
| 登录 | `/login` | `templates/login.html` | `auth.js` | `/api/auth/*` | auth UI/routes |
| 设置 | `/settings` | Gateway内页面 | inline/static | `/api/settings*` | settings tests |
| Browser V2 | `/browser-v2` | `templates/browser_v2.html` | `browser_v2.js/css` | `/api/browser-v2/*` | browser-v2 UI |
| Comment Campaign | `/comment-campaigns` | `templates/comment_campaign.html` | `comment_campaign.js/css` | comment-* | campaign UI |
| Selector Probe | 首页内模块/Probe路由 | `_selector_probe_console.html` | `selector_probe_ui.js/css`、inventory | `/api/selector-probe/*` | selector UI suites |
| TikTok Stats | `/tiktok-stats` | `templates/tiktok_stats.html` | `tiktok_stats.js/css` | `/api/tiktok-stats/*` | tiktok stats UI |

旧账号、内容、发布和Legacy Browser页面部分仍由`gateway/app.py`内大段HTML/JS提供。修改前用路由清单和`tests-js`定位，不假设已有独立Template。
