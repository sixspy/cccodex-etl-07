# cc-etl-lab

CC_Codex 二期（26.11）**数据工程**产品线：**抽取转换加载台**（FastAPI + Vue 3 SPA + SQLite）。

与 `cc-data-lab`（Flask + HTMX 采集控制台）错开：本线面向 **ETL 管道编排 / 抽取落库**，前端为 Vue 单页，暖色仓运风格。

## 领域

- **所属领域**：数据工程  
- **子方向**：抽取-转换-加载 / 数据采集与落库  

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、FastAPI、Uvicorn、SQLite |
| 前端 | Vue 3（CDN Composition）、双栏管道台 |
| 风格 | 暖琥珀仓运底、圆角强调、等宽源地址 |
| 测试 | pytest + httpx |

## 基线能力

登录、管道列表、管道详情、执行抽取、执行清洗、执行落库。

## 本轮：宽表 / 窄表落库形态

流程：**待抽取 → 已抽取待清洗 → 已清洗待落库 →（落库中）→ 已落库 / 部分落库 / 落库失败**。

- **落库方案册**：每条管道的落库形态（宽表 `wide` / 窄表 `narrow`）定一种写进说明（`shape_specs`），
  含说明名、指标清单、宽表必填列、决定日；`GET /api/shape-specs` 一览。
- **形态设置**：`PUT /api/pipelines/{id}/shape`。形态写进说明后**本日禁止改口**（次日可改）；
  **落库进行中禁止改形态**。
- **形态预览**：`GET /api/pipelines/{id}/preview`。形态由服务端落地，前端对照渲染——
  宽表预览为**打成 1 行**（指标作列），窄表预览为**按指标拆 N 行**，页面可分清两种形态。
- **落库**：`POST /api/pipelines/{id}/load`（可带 `{"shape": "wide|narrow"}`）。
  - **必须清洗通过后才能落库**，否则 400「未清洗不能落库」。
  - 落库形态若偏离说明，400「**落库形态写歪了**」并记一条**形态冲突**。
  - **宽表**缺任一必填列**整批失败**（422）；**窄表**允许部分指标成功，但须列出**未落指标**。
  - 同一管道两人抢着落库，**只让先到成功**；后到 409 记一条**形态争用**，
    抢占记录在预览与详情都留痕。
- **管道列表**：显示形态名、将落行数、未落指标、说明名。
- **管道详情**：可读形态冲突与争抢记录、最近落库结果。

## 本地

```powershell
cd claude-workspace\cc-etl-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:6000/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **6000**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
