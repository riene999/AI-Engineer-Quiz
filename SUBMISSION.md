# 交付说明：素材检索与结果导出

- Fork：`<填写 fork 链接>`
- 分支：`feature/search-filter-export`
- 最终 commit：`<填写 SHA>`

## 运行与验证

```bash
uv venv --python 3.12
uv pip install -e '.[dev]'
export KEEPSTACK_AI_ENABLED=false
export KEEPSTACK_OCR_ENABLED=false

uv run --no-sync python -m keepstack seed            # 原有 24 个演示素材
uv run --no-sync python -m keepstack seed-scenario   # 新增：业务场景数据（可重复执行）
uv run --no-sync python -m keepstack                 # http://localhost:8000  admin / admin

uv run --no-sync python -m pytest -q
```

页面验收路径：Library 页 → 搜索框输入 `harbor` → Collection 选 `Harbor Campaign` →
Must have tags 输入 `approved`、`campaign`（各回车）→ Exclude tags 输入 `internal` →
Sort 选 `Created: oldest first` → 应显示 26 条，共两页（每页 24）→ 点 Export CSV。

## 实测结果

- `pytest`：34 passed, 2 skipped（跳过的 2 个是原有的可选 CLIP/OCR 测试）。
- 新增的 11 个测试中，有 10 个在改动前的代码上失败（只有鉴权那个原本就能通过）。
- 在 seed + seed-scenario 的库上，用 HTTP 跑了上面的验收路径：总数 26，第 1 页 24 条、
  第 2 页 2 条；CSV 26 行，顺序与列表一致，中文、逗号、引号、换行都能正确解析回来；
  未登录访问返回 401。

## 实现概要

**API**（`keepstack/app.py`）

- `GET /api/assets` 和 `GET /api/assets/export.csv` 共用 `search_filters` 依赖：
  `q`、`collection_id`、`media_type`、`tag`（可重复，须全部包含）、`exclude_tag`（可重复，任一命中即排除）、
  `sort`（`newest`/`oldest`/`name`/`largest`）。
- 列表另有 `limit`（1–200）和 `offset`（≥0），非法值返回 422。
- 只返回 `active` 素材，原来的 `status` 查询参数已移除。

**查询**（`keepstack/search.py`）：列表和导出共用 `_filter_sql`，保证两者条件完全一致。

- 所有条件都写成 `assets` 上的 WHERE 谓词（`IN`/`EXISTS`/`NOT EXISTS`/计数子查询），不用 JOIN，
  因此不会产生重复行，参数绑定顺序也和 SQL 一致。
- 标签输入按存储方式规范化（`strip` + 小写），并去重。同一标签同时包含和排除时，SQL 自然返回空。
- 每种排序都以 `a.id` 作为最后的排序键，保证顺序全序：`created_at` 相同时分页也不会重复或遗漏。
- 有关键词时同样使用所选排序，不再隐式按 bm25 排。

**导出**

- CSV 使用 UTF-8 BOM（Excel 打开中文不乱码），按 RFC 4180 加引号，行尾 CRLF。
- 零条结果时只输出表头。
- 多标签按名称排序，用 `;` 连接，例如 `approved;campaign`。
- 每次导出都会写审计日志。

**前端**（`web/`）

- 筛选栏：集合、类型、包含标签、排除标签（带标签自动补全）、排序。
- 生效条件以可单独移除的标签块展示，另有 Clear all。
- 上一页/下一页，显示"Showing x–y of N"。
- 请求带序号，并 abort 上一个请求，保证最终显示的是最新条件的结果。
- 空结果和加载失败分开展示，加载失败可重试。
- 导出时用 fetch 携带 Bearer token，下载 Blob，失败时弹 toast。
- 隐藏了语义搜索入口。
- 侧栏的计数明确标注为"全库数量"，不是当前匹配数。

## 修复的已有缺陷

1. **标签/集合筛选参数错位**：原 `search()` 把 JOIN 里的 `?` 参数追加在 WHERE 参数之后，但 SQL 中 JOIN 在前，
   结果是只要按标签或集合筛选（侧栏点标签、从集合进入）就返回空。
2. **有关键词时忽略排序**：强制按 bm25 排序。
3. **排序不稳定**：只按 `created_at` 排，时间相同时分页可能重复或遗漏。
4. **详情页改标签后列表不刷新**：只刷新了抽屉，没有重新加载列表。

## 主要取舍

- **不新增依赖或组件**：只用标准库 `csv`；前端保持原生 JS、无构建步骤。
- **offset 分页而非游标分页**：题目只要求数据不变时稳定，`ORDER BY …, id` 已经足够；实现简单，也方便跳页。
- **导出一次在内存里生成**：没有用流式响应。原有连接是按线程缓存的，流式生成器可能跨线程使用连接，有风险。
  当前数据规模下内存不是问题。
- **导出按"当前已显示列表"的条件**：如果用户刚改了条件、新列表还没加载回来，导出的是已经显示的那份结果，
  不会和屏幕上看到的不一致。
- **关键词处理沿用原逻辑**：`_fts_query` 仍为前缀匹配（`harbor` 也会命中 `harbors`），检索字段包括
  标题/描述/alt/文件名/标签/元数据/自定义字段。只含标点、分不出词的输入按空关键词处理。
- **测试数据**：测试直接写 SQLite，并调用真实的 `reindex_fts`（`tests/factory.py`）。每个测试有自己的随机命名空间，
  与共享测试库里的其他数据互不干扰。`seed-scenario` 走真实的 ingest 流程，生成带缩略图的演示数据。

## 限制与未完成项

- **前端没有在真实浏览器里点过**：开发环境没有浏览器自动化工具。已验证 JS 语法（`node --check`）、
  页面实际调用的各个接口，以及静态资源的加载；交互细节（例如快速切换条件时的视觉表现）需要人工确认。
- 前端没有自动化测试，"快速切换条件只显示最新结果"靠请求序号 + AbortController 保证，没有测试覆盖。
- 标签名本身含 `;` 时，CSV 的 `tags` 列会有歧义。
- CSV 没有处理公式注入（以 `=`/`+`/`-`/`@` 开头的字段）：处理的话会改动原始数据，这里保持原样输出。
- 中文关键词受 FTS5 `unicode61` 分词所限：连续的中文会作为一个整词，只支持前缀匹配，不支持词中子串。
- 筛选状态没有同步到 URL，刷新页面后条件会丢失。

## 范围外问题（仅记录）

- `DELETE /api/assets/{uuid}/tags/{name}` 不更新 embedding，也不写审计日志；
  标签名含 `/` 时无法通过这个路径删除。
- `/api/tags` 的计数包含已删除的素材。
- 语义搜索的后端路径不支持集合/标签筛选（入口已隐藏）。
- toast 的定时器会互相覆盖，连续提示时可能提前消失。
- FastAPI 的 `on_event` 已废弃，有 deprecation warning。
