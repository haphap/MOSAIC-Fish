<div align="center">

# 🐟 MOSAIC-Fish

**Self-hosted multi-agent prediction engine — a Neo4j graph-memory fork of [MiroFish](https://github.com/666ghj/MiroFish).**

[![Upstream](https://img.shields.io/badge/upstream-666ghj%2FMiroFish-blue?style=flat-square&logo=github)](https://github.com/666ghj/MiroFish)
[![Neo4j](https://img.shields.io/badge/Neo4j-Community%205.x-008CC1?style=flat-square&logo=neo4j&logoColor=white)](https://neo4j.com/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker&logoColor=white)](https://docs.docker.com/compose/)

[English](./README.md) | [中文](./README-ZH.md)

</div>

## What is this?

[MiroFish](https://github.com/666ghj/MiroFish) is a multi-agent engine that builds a high-fidelity digital world from seed material (news, policy drafts, financial signals) and simulates how a crowd of agents reacts, to forecast outcomes.

**MOSAIC-Fish** keeps that product and workflow, but changes only the **graph-memory backend** so the whole stack runs self-hosted:

- **Neo4j Community Edition replaces Zep Cloud** — LLM entity/edge extraction + vector & full-text **hybrid search (RRF)** on a local Neo4j container. No Zep account needed.
- **No Ollama lock-in** — LLM *and* embeddings use configurable OpenAI-format APIs (`LLM_*` / `EMBEDDING_*`), e.g. DashScope (`qwen-plus` + `text-embedding-v4`) or OpenAI.
- **Drop-in shim** — a `MiroGraph` client mirrors Zep's `client.graph.*` surface, so upstream stays easy to merge. Set `GRAPH_MEMORY_BACKEND=zep` to fall back to Zep.

## Quick Start

**Prerequisites:** Docker (and, for source runs, Node.js 18+ · Python 3.11–3.12 · uv).

### 1. Configure

```bash
cp .env.example .env
```

```env
# LLM (OpenAI-format API, e.g. Alibaba Bailian)
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL_NAME=qwen-plus

# Embedding (OpenAI-format /embeddings; custom-dimension model)
EMBEDDING_API_KEY=your_api_key
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL_NAME=text-embedding-v4
EMBEDDING_DIMENSIONS=1536

# Neo4j graph memory
GRAPH_MEMORY_BACKEND=neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=mirofishdev
```

### 2. Run with Docker Compose (recommended)

```bash
docker compose up -d --build
```

Starts Neo4j and builds the app from source. Ports: `3000` frontend · `5001` backend · `7474` Neo4j Browser · `7687` Bolt. Inside Compose the app reaches Neo4j at `bolt://neo4j:7687` automatically.

### Or run from source

```bash
# Neo4j (graph memory) — auto-creates vector/full-text indexes on first connect
docker run -d --name mirofish-neo4j -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/mirofishdev -v mirofish-neo4j-data:/data neo4j:5-community

npm run setup:all   # install deps (root + frontend + backend)
npm run dev         # start frontend (3000) + backend (5001)
```

## Acknowledgments

- **[666ghj/MiroFish](https://github.com/666ghj/MiroFish)** — the upstream project (incubated by Shanda Group); all credit for the original engine goes to the MiroFish team.
- **[OASIS](https://github.com/camel-ai/oasis)** by CAMEL-AI — the social-simulation engine.
- **[Neo4j Community Edition](https://neo4j.com/)** — the graph-memory backend.
