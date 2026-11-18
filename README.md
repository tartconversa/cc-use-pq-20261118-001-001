# Meridian 区域媒体证据分析服务

本服务供研究团队对本地提供的媒体观察记录进行地区、议题和受众比较。系统不抓取网页、不自动推断报道倾向，也不声称合成演示能代表真实群体。

来源登记绑定地区、媒体和允许词表；观察记录按来源和外部编号保留修订。两名研究员独立编码，分歧由第三人裁定。按地区、故事、议题和受众去重，报告显示分母、排除原因和原始证据数。

故事身份不经自动推断。来源提供的 `story_key` 只描述单篇稿件；研究员用 `POST /story-links` 明确登记跨来源成员集合（认定人、理由、成员快照），并用 `POST /story-links/{link_id}/revoke` 撤销。未获认定的稿件即使标题或编号相似也不会被合并。撤销后稿件回到各自的原始 `story_key`，认定与撤销都保留事件记录。

报告的 `story_count` 与 `stance_counts` 反映认定后的结果，同时给出 `source_key_story_count`、`before_determination` 与 `after_determination`，并在 `determination` 中给出两者差值与影响到的稿件数。地区比较、来源贡献和议题矩阵同样保留认定前分母。认定成员后续更正、撤回或重新编码时，报告按当前数据重算成员，不沿用旧代表稿件；已发布报告保持冻结，`GET /releases/{release_id}/changes` 会列出认定成员的增减与撤销理由。`POST /reports/readiness` 将未解决的倾向冲突和认定冲突列为阻断项。

使用 `python -m meridian.api --db meridian.sqlite3` 启动 FastAPI。主要接口包括 `POST /sources`、`POST /observations`、`POST /decisions`、`POST /adjudications`、`POST /story-links`、`POST /batches`、`POST /reports` 和 `POST /releases`。故事追溯使用 `POST /reports/story`；采集窗口使用 `/frames`，导出使用 `/reports/export` 或 `/releases/{release_id}/export`。

运行 `python -m unittest discover -s tests -v`、`python -m compileall -q meridian` 和 `python -m meridian.demo`。数据库使用 SQLite；测试和演示使用临时本地数据库。
