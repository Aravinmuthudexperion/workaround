# Sample client agent

`claims_agent.py` plays the role of a client team's AI application. It uses only the
standard OpenTelemetry SDK and has no dependency on ARC Tower. Everything ARC-specific
is in the environment:

```bash
# Windows PowerShell: Get-Content agent.env | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { Set-Item "env:$($matches[1])" $matches[2] } }
set -a; source agent.env; set +a          # macOS / Linux
python claims_agent.py --minutes 5 --rate 2
```

`python start.py demo` does this for you: it creates a demo tenant and product, writes
`agent.env` with a fresh ingest key, and runs the agent.

Options:

| Flag | Meaning |
|---|---|
| `--minutes` | How long to run (default 5) |
| `--rate` | Conversations per second (default 1) |
| `--degrade-after N` | After N minutes, latency, tokens and errors rise, to demonstrate drift/anomaly alerts |

The LLM and tools are simulated, so no model API key is needed. Some prompts contain an
e-mail address or card number on purpose: ARC Tower masks them before storage.

## With a real LangChain app instead

```bash
pip install opentelemetry-distro opentelemetry-exporter-otlp openinference-instrumentation-langchain
set -a; source agent.env; set +a
opentelemetry-instrument python your_app.py
```
