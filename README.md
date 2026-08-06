# astrbot_plugin_qweather

> ⚠️ **旧版用户注意：** 本插件由 `astrbot_plugin_weather` 更名为 `astrbot_plugin_qweather`。使用旧版插件的用户请先**卸载旧插件**并**删除旧插件配置**，再安装本插件；v2.x 升级到 v3.x 同样建议先卸载重装，避免配置或缓存目录冲突。

基于**高德地图地理编码** + **和风天气 API** 的 AstrBot 天气查询插件，支持命令调用与 LLM Tool Calling 双模式（三个独立天气工具），天气数据经 LLM 人格化润色后输出，自然流畅。

---

## ✨ 功能特性

| 特性 | 说明 |
|------|------|
| 🌍 智能地理编码 | 基于高德地图 API，支持城市、区县、街道等多级地址解析 |
| 🌤️ 实时天气 | 温度、体感温度、湿度、风向风速、气压、能见度、紫外线指数 |
| 📅 每日预报 | 未来 1~7 天白天/夜间天气、最高最低温、降水概率 |
| ⏰ 逐小时预报 | 未来 1~24 小时逐小时天气趋势 |
| 🤖 三个独立 LLM 工具 | `get_current_weather`（实时）/ `get_daily_forecast`（每日）/ `get_hourly_forecast`（逐小时），LLM 按用户意图精确调用对应工具 |
| 🎭 人格化输出 | 天气数据经 LLM 重新组织语言，符合机器人人设风格 |
| ⚡ 并发请求 | 实时/每日/逐小时三路请求并发执行，响应更快 |
| 💾 地理编码缓存 | 查询过的地点长期保存至文件（不占内存，条数可配置，默认 20 条，超出自动覆盖最早记录），重复查询不再请求高德 API |

---

## 📦 安装

### 方式一：AstrBot 插件市场（推荐）

1. 进入 AstrBot 控制台 → 插件市场
2. 搜索 qweather
3. 点击安装，重启 AstrBot

### 方式二：手动安装

1. 将插件文件夹放入 data/plugins/ 目录
2. 重启 AstrBot

---

## ⚙️ 配置

安装后进入 **控制台 → 插件管理 → astrbot_plugin_qweather → 设置**，填写以下配置项：

| 配置项 | 必填 | 说明 | 示例 |
|--------|------|------|------|
| qweather_api_key | ✅ | 和风天气 API Key | a1b2c3d4e5f6... |
| qweather_api_host | ✅ | 和风天气 API Host（见下方说明） | api.qweather.com |
| amap_api_key | ✅ | 高德地图 Web 服务 API Key | f7e8d9c0b1a2... |
| default_forecast_days | ❌ | 每日预报天数（1~7），默认 3 | 3 |
| default_hourly_hours | ❌ | 逐小时预报小时数（1~24），默认 12 | 12 |
| request_timeout | ❌ | 请求超时秒数，默认 15 | 15 |
| geo_cache_max_entries | ❌ | 地理编码缓存最大保存条数，默认 20（0 表示不限制），超出自动覆盖最早保存的记录 | 20 |
| persona_prompt | ❌ | 自定义人设提示词（留空则使用默认友好风格） | 你是一个傲娇猫娘，说话带喵~ |

### 关于 qweather_api_host

| 订阅类型 | Host |
|----------|------|
| 免费订阅（开发版） | devapi.qweather.com |
| 标准订阅 | api.qweather.com |
| 高级订阅 | api.qweather.com |

> 💡 具体以你申请 API Key 时和风天气官方文档为准。

### 关于 persona_prompt

此配置用于控制 LLM 输出天气信息时的语气和风格。示例：

    你是一位温柔体贴的邻家姐姐，关心用户的日常起居，说话语气柔和，偶尔用"呢""哦""啦"等语气词。

留空则使用默认的亲切自然风格。

---

## 🚀 使用方式

### 方式一：命令调用

    /weather 北京
    /weather 上海浦东新区
    /天气 赣州

插件将自动完成：地理编码 → 获取天气 → LLM 人格化润色 → 输出。

### 方式二：LLM Tool Calling（自动触发）

用户直接用自然语言询问天气即可：

    今天北京天气怎么样？
    明天赣州会下雨吗？
    出门要带伞吗？
    这周末适合户外活动吗？

LLM 会自动识别天气意图并调用对应的天气工具（`get_current_weather` / `get_daily_forecast` / `get_hourly_forecast`），无需手动输入命令。

> 💡 如果 LLM 未触发天气工具而选择了网页搜索，可在 AstrBot 的 **LLM 系统提示词** 中追加：
> 当用户询问天气相关问题时，必须优先使用 get_current_weather、get_daily_forecast、get_hourly_forecast 工具，禁止使用搜索工具。

---

## 📋 输出示例

    赣州的朋友，晚上好呀～🌙
    
    现在外面多云，气温 27.3°C，不过体感有点闷热（30.3°C），湿度也挺大的。
    风倒是轻轻的，不用担心被吹跑啦～
    
    未来三天多阵性小雨哦，特别是后天降水概率比较高，出门记得带伞☂️
    白天最高温还在 33~35°C 徘徊，注意防暑补水！

---

## ❓ 常见问题

### Q: 提示"和风天气 API Key 未配置"？
A: 请确认已在插件设置中填写 qweather_api_key，且重启了 AstrBot。

### Q: 提示"无法找到地点"？
A: 检查 amap_api_key 是否正确，以及高德 API 是否开通了 **Web 服务-地理编码** 权限。

### Q: 天气数据获取超时？
A: 尝试增大 request_timeout 值，或检查服务器到和风天气 API 的网络连通性。

### Q: LLM 输出没有体现人设？
A: 确认 persona_prompt 已填写，且当前使用的 LLM 模型支持 system prompt 注入。部分模型对 tool 返回内容的指令遵循度较低，可尝试换用指令遵循能力更强的模型。

### Q: 从 v1.x.x 升级后报错？
A: 请先卸载插件 → 删除 data/plugins/astrbot_plugin_qweather 目录及对应配置文件 → 重新安装。

### Q: 如何清空地理编码缓存？
A: 直接删除文件 `data/plugin_data/astrbot_plugin_qweather/geo_cache.json` 即可，下次查询会自动重新建立缓存。

---

## 📁 项目结构

    astrbot_plugin_qweather/
    ├── main.py              # 插件主逻辑
    ├── metadata.yaml        # 插件元信息（名称、版本）
    ├── _conf_schema.json    # 配置项声明（插件设置面板生成依据）
    ├── requirements.txt     # 依赖声明
    └── README.md            # 本文件

---

## 📄 依赖

- aiohttp（AstrBot 已内置，无需额外安装）

---

## 📝 更新日志

### v3.1.1
- ✨ 统一版本号为 3.1.1，正式发布
- 🛠️ 修复插件配置面板中配置项描述不显示的问题（_conf_schema.json 对齐标准格式，补全 hint 提示）
- 📝 更新 README 文档说明（配置、工具、缓存、FAQ）

### v3.0.3
- ✏️ 插件更名为 astrbot_plugin_qweather，仓库地址更新

### v3.0.2
- 💾 地理编码缓存改为纯文件长期保存（不占内存，条数可配置，默认 20 条，超出自动覆盖最早记录）

### v3.0.0
- 🔀 拆分 LLM 工具为三个独立工具：get_current_weather / get_daily_forecast / get_hourly_forecast

### v2.0.1
- 🆕 支持 LLM Tool Calling

### v1.x.x
- 初始版本（已停止维护）


---

## 🙏 致谢

- [和风天气](https://www.qweather.com/) - 提供气象数据 API
- [高德地图](https://lbs.amap.com/) - 提供地理编码服务
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) - 插件运行框架