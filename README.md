# BRD Agent

Local PoC for a BRD generation agent with:

- FastAPI backend on `http://localhost:8010`
- Streamlit frontend on `http://localhost:8501`
- Phoenix traces on `http://localhost:6006`
- Neo4j knowledge graph on `bolt://localhost:7687`
- Chroma persisted locally under `storage/chroma`

## Windows Setup

Use Python 3.11 for the local virtual environment.

```powershell
.\scripts\setup.ps1
```

Then edit `.env` and replace the Azure OpenAI placeholders:

```dotenv
AZURE_OPENAI_ENDPOINT=https://your-endpoint.services.ai.azure.com/
AZURE_OPENAI_API_KEY=REPLACE_ME
AZURE_OPENAI_DEPLOYMENT=gpt-4o
```

The Neo4j password is already aligned with Docker Compose:

```dotenv
NEO4J_USER=neo4j
NEO4J_PASSWORD=brd_agent_neo4j_pw
```

Start everything:

```powershell
.\scripts\run.ps1
```

Validate startup and stop automatically:

```powershell
.\scripts\run.ps1 -SmokeTest
```

Optional seeds after dependencies are installed:

```powershell
.\.venv\Scripts\python.exe -m scripts.seed_chroma
cd infra
docker compose up -d neo4j
cd ..
.\.venv\Scripts\python.exe -m scripts.seed_neo4j
```

## Linux Setup

The existing Bash orchestrator is still available:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
./scripts/run.sh
```
