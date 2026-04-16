# Bolt SC — 原材料价格监控 Dashboard v2

每周自动爬取中国现货价格，生成静态 HTML Dashboard，通过 GitHub Pages 发布。

## 监控材料 & 数据源

| 材料 | 数据源 | 抓取方式 |
|------|--------|----------|
| A00 铝锭 | 世铝网 cnal.com（长江有色） | requests |
| 1# 电解铜 | 世铝网 cnal.com（长江有色） | requests |
| 金属钕 | dailymetalprice.com | requests |
| PC 树脂 | 生意社 100ppi.com | Playwright 无头浏览器 |
| ABS 树脂 | 生意社 100ppi.com | Playwright 无头浏览器 |
| 取向硅钢 | 生意社 100ppi.com | Playwright 无头浏览器 |

## 部署步骤

1. 创建 GitHub 仓库，上传所有文件（注意 `.github/workflows/` 路径）
2. Settings → Pages → Branch: main, Folder: /docs → Save
3. Settings → Actions → General → Workflow permissions: Read and write → Save
4. Actions → Update Price Dashboard → Run workflow
