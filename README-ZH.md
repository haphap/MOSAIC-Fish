<div align="center">

# 🐟 MOSAIC-Fish

**可本地自托管的多智能体预测引擎 —— [MiroFish](https://github.com/666ghj/MiroFish) 的 Neo4j 图记忆分支。**

[![Upstream](https://img.shields.io/badge/upstream-666ghj%2FMiroFish-blue?style=flat-square&logo=github)](https://github.com/666ghj/MiroFish)
[![Neo4j](https://img.shields.io/badge/Neo4j-Community%205.x-008CC1?style=flat-square&logo=neo4j&logoColor=white)](https://neo4j.com/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker&logoColor=white)](https://docs.docker.com/compose/)

[English](./README.md) | [中文](./README-ZH.md)

</div>

## 这是什么？

[MiroFish](https://github.com/666ghj/MiroFish) 是一款多智能体引擎：从种子材料（新闻、政策草案、金融信号）构建高保真数字世界，模拟群体反应来推演结果。

**MOSAIC-Fish** 保留其产品与工作流，仅替换**图记忆后端**，让整套系统可本地自托管：

- **用 Neo4j Community Edition 取代 Zep Cloud** —— LLM 实体/关系抽取 + 向量与全文 **RRF 混合检索**，跑在本地 Neo4j 容器，无需 Zep 账号。
- **不绑定 Ollama** —— LLM 与 Embedding 均走可配置的 OpenAI 格式 API（`LLM_*` / `EMBEDDING_*`），如阿里百炼（`qwen-plus` + `text-embedding-v4`）或 OpenAI。
- **Drop-in shim** —— `MiroGraph` 客户端复刻 Zep 的 `client.graph.*` 调用面，便于持续合并上游；设 `GRAPH_MEMORY_BACKEND=zep` 可切回 Zep。

## 快速开始

**前置：** Docker（源码运行另需 Node.js 18+ · Python 3.11–3.12 · uv）。

### 1. 配置

```bash
cp .env.example .env
```

```env
# LLM（OpenAI 格式 API，如阿里百炼）
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL_NAME=qwen-plus

# Embedding（OpenAI 格式 /embeddings，支持自定义维度的模型）
EMBEDDING_API_KEY=your_api_key
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL_NAME=text-embedding-v4
EMBEDDING_DIMENSIONS=1536

# Neo4j 图记忆
GRAPH_MEMORY_BACKEND=neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=mirofishdev
```

### 2. Docker Compose 运行（推荐）

```bash
docker compose up -d --build
```

会拉起 Neo4j 并从源码构建应用。端口：`3000` 前端 · `5001` 后端 · `7474` Neo4j Browser · `7687` Bolt。容器内应用通过 `bolt://neo4j:7687` 自动访问 Neo4j。

### 或从源码运行

```bash
# Neo4j（图记忆）—— 首次连接自动创建向量/全文索引
docker run -d --name mirofish-neo4j -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/mirofishdev -v mirofish-neo4j-data:/data neo4j:5-community

npm run setup:all   # 安装依赖（根目录 + 前端 + 后端）
npm run dev         # 启动前端(3000) + 后端(5001)
```

## 致谢

- **[666ghj/MiroFish](https://github.com/666ghj/MiroFish)** —— 上游项目（由盛大集团孵化），原始引擎的全部功劳归于 MiroFish 团队。
- **[OASIS](https://github.com/camel-ai/oasis)**（CAMEL-AI）—— 社会模拟引擎。
- **[Neo4j Community Edition](https://neo4j.com/)** —— 图记忆后端。
