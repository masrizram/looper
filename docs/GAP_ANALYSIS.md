# Looper — Gap Analysis & Roadmap Rekomendasi

> **Status:** Dokumentasi saja, belum dieksekusi  
> **Tanggal:** 2026-08-09  
> **Target Versi:** v2.3.0 (post v2.2.0)

---

## 1. LLM Provider Abstraction (OpenRouter → Multi-Provider)

### Gap Saat Ini
`looper/llm.py` (`OpenRouterClient`) men-support **hanya OpenRouter** sebagai LLM endpoint. Semua konfigurasi provider, model tier, dan API key hard-coded pada pola OpenRouter.

### Bukti
```
looper/llm.py:15:    def __init__(self, api_key: str, base_url: str = "https://openrouter.ai/api")
looper/llm.py:25:        "x-openrouter-routing": self._get_routing(prefs),
```

### Rekomendasi Teknis
1. Definisikan ABC `LLMProvider` dengan metode:
   - `async def request(self, messages, model, **kwargs) -> LLMResponse`
2. Refactor `OpenRouterClient` → `OpenRouterProvider(LLMProvider)`
3. Tambah provider baru:
   - `AnthropicProvider` (Claude 3.5/4)
   - `OpenAIProvider` (GPT-4.1/5)
   - `GeminiProvider` (Gemini 2.0/2.5)
   - `BedrockProvider` (Claude via AWS)
4. Factory pattern di `config.py`:
   ```python
   provider = LLMProviderFactory.create(config.llm.provider, config.llm.api_key)
   ```
5. Update `config.yaml` schema:
   ```yaml
   llm:
     provider: openai  # openrouter | anthropic | openai | gemini | bedrock
     api_key_env: OPENAI_API_KEY
     base_url: https://api.openai.com/v1
   ```

### Dampak
- **Resiliensi:** tidak tergantung satu vendor; failover ke Claude saat OpenRouter down
- **Compliance:** beberapa industri membatasi OpenRouter; provider langsung diperlukan
- **Cost optimization:** route prompt ke model provider termurah yang memenuhi SLA score

### Estimasi Usaha
- **Medium (3-5 hari dev)** — refactor kecil, tapi menambah provider baru butuh testing akurat

---

## 2. Structured Output untuk Scoring

### Gap Saat Ini
Reviewer dan Security_Auditor mengembalikan **prose text**, lalu `regex`/parsing diekstrak untuk score. Ini brittle — format prose berubah antar model, menyebabkan score extraction gagal.

### Bukti
```
looper/phases/review.py:78:    match = re.search(r"Review Score:\s*(\d+)", result.content, re.IGNORECASE)
looper/phases/security_audit.py:45:    issues = self._extract_issues(result.content)
```

### Rekomendasi Teknis
1. Tambahkan instruct di prompt:
   ```
   Return ONLY valid JSON in this schema:
   {"score": 85, "summary": "...", "details": {"strengths": [...], "issues": [...]}}
   ```
2. Gunakan Python `json.loads()` — jika gagal, log dan gunakan regex fallback
3. Definisikan Pydantic schema:
   ```python
   class ReviewScore(BaseModel):
       score: float
       summary: str
       strengths: list[str]
       issues: list[str]
   ```
4. Update semua phase yang mengembalikan score: reviewer, security auditor, performance optimizer

### Dampak
- **Reliability:** skrening format tak bergantung pada prose wording
- **Debuggability:** structured output bisa langsung divisualisasikan
- **Extensibility:** bisa tambah field tanpa pasing regex

### Estimasi Usaha
- **Small (1-2 hari dev)** — update prompt + parsing logic

---

## 3. Webhook Retry Mechanism

### Gap Saat Ini
`looper/notify.py` mengirim webhook ke Slack/Discord/Mattermost, tapi **hanya satu kiriman** — tidak ada retry, tidak ada dead letter queue, tidak ada exponential backoff.

### Bukti
```
looper/notify.py:42:    async with aiohttp.ClientSession() as session:
looper/notify.py:44:            await session.post(url, json=payload, headers=headers, timeout=10)
```

### Rekomendasi Teknis
1. Tambahkan retry dengan Exponential Backoff + Jitter:
   - Max 5 retry attempts
   - Base backoff: 2 detik, kenaikan eksponensial (`2^n + jitter`)
2. Dead letter queue di state file:
   ```python
   state["webhook_dlq"] = [{"timestamp": "...", "url": "...", "payload": "...", "error": "..."}]
   ```
3. Circuit breaker: jika 3 webhook gagal berturut-turut, pause kirim selama 30 detik
4. Support partial success (misal: Discord kirim, Slack gagal → catat yang gagal)

### Dampak
- **Reliability:** notification penting (build fail/cost alert) tak akan hilang
- **Production readiness:** sistem harus tahan terhadap endpoint downstream yang flak

### Estimasi Usaha
- **Medium (2-3 hari dev)** — tambah retry logic + DQL + circuit breaker

---

## 4. Cost Alerting (80%/90% Budget Threshold)

### Gap Saat Ini
Looper sudah punya `cost_ceiling` yang hard-enforced, tapi **hanya notifikasi sekali ketika ceiling terlewihi** (exit code 4). Tidak ada peringatan progresif pada 80% atau 90%.

### Bukti
```
looper/orchestrator.py:287:    if self._spent >= self.config.cost_ceiling:
looper/orchestrator.py:289:        logger.error("Cost budget exceeded: $%.2f >= $%.2f", self._spent, self.config.cost_ceiling)
```

### Rekomendasi Teknis
1. Tambahkan configurable alert thresholds di `config.yaml`:
   ```yaml
   cost_alerting:
     thresholds: [0.80, 0.90]
     webhook_url: "${WEBHOOK_URL}"
     channels: ["slack", "discord"]
   ```
2. Di orchestrator, setiap kali `_spent` melewati sebuah threshold:
   - Cek apakah sudah pernah notifikasi threshold tertentu (track di `state["cost_alerts_sent"]`)
   - Jika belum, kirim webhook dengan progress bar
3. Payload webhook:
   ```json
   {"event": "cost_threshold", "threshold": 0.80, "spent": 7.20, "ceiling": 9.00}
   ```

### Dampak
- **Proactive cost management:** tim bisa bereaksi sebelum budget habis
- **Trust building:** stakeholder tidak kejutan ketika build tiba-tiba berhenti

### Estimasi Usaha
- **Small (1 hari dev)** — tambah tracker threshold + webhook call

---

## 5. Phase Plugin System

### Gap Saat Ini
Phases (research, architect, build, test, review, security, etc.) **hardcoded** di `looper/phases/__init__.py` dan dijalankan berurutan/di-orchestrator.py. Menambah phase baru butuh modifikasi langsung di kode.

### Bukti
```
looper/phases/__init__.py:3:    "research", "architect", "build", "test", "review",
looper/phases/__init__.py:4:    "security_audit", "performance", "documentation", "fix"
```

### Rekomendasi Teknis
1. Definisikan `PhasePlugin` ABC:
   ```python
   class PhasePlugin(ABC):
       name: str
       priority: int
       
       @abstractmethod
       async def execute(self, ctx: BuildContext) -> PhaseResult: ...
   ```
2. Gunakan `importlib.metadata.entry_points()` untuk discovery:
   ```toml
   # setup.cfg / pyproject.toml
   [project.entry-points."looper.phases"]
   my_custom_phase = "my_plugin.module:MyPhase"
   ```
3. Registry di orchestrator — urutkan by priority, jalankan secara paralel jika tidak bergantung
4. External phase bisa di-`pip install` terpisah

### Dampak
- **Extensibility:** komunitas bisa menambah phases tanpa fork repo
- **Modularity:** setiap phase menjadi unit yang dapat diuji dan di-deploy mandiri
- **Ecosystem:** ecosystem seperti CrewAI/AutoGen sudah maksuk di sini

### Estimasi Usaha
- **Large (5-7 hari dev)** — redesign arsitektur phase dispatching

---

## 6. OpenTelemetry Integration

### Gap Saat Ini
Looper tidak ada tracing apa pun. Logging ada (`logging` module), tapi tidak bisa di-correlate antar request/phase/LLM call.

### Bukti
```
looper/orchestrator.py:15:    logger = logging.getLogger("looper.orchestrator")
```
Tidak ada span, trace ID, atau metric export.

### Rekomendasi Teknis
1. Tambahkan OpenTelemetry SDK (runtime minimal — opsi `pip install looper[otel]`):
   ```python
   from opentelemetry import trace
   from opentelemetry.sdk.trace import TracerProvider
   tracer = trace.get_tracer(__name__)
   
   with tracer.start_as_current_span("phase.review"):
       await self._run_reviewer()
   ```
2. Span untuk setiap layer:
   - Phase execution (`phase.build`, `phase.test`)
   - LLM call (`llm.openrouter.request`)
   - Sandbox execution (`sandbox.run`)
   - State save (`state.save`)
3. Export ke:
   - Console (development): `ConsoleSpanExporter`
   - OTLP (production): `OTLPSpanExporter` → Jaeger/Zipkin/tempo
4. Tambahkan atribut:
   - `looper.goal` = "build a CLI todo app"
   - `looper.cycle` = 5
   - `looper.score` = 69.4
   - `looper.cost` = 0.02

### Dampak
- **Observability:** root cause analysis 10x lebih cepat
- **Performance profiling:** bottleneck terlihat jelas (LLM call vs sandbox vs state save)
- **Enterprise readiness:** wajib untuk deployment produksi

### Estimasi Usaha
- **Medium (3-4 hari dev)** — tambah SDK + span instrumentation

---

## 7. (Baru) Prometheus Metrics Endpoint

### Gap Saat Ini
Tidak ada metrics endpoint untuk pemantauan sistem (CPU, memory, LLM cost rate, build success rate).

### Rekomendasi Teknis
1. Endpoint `/metrics` (Prometheus format):
   ```
   looper_build_duration_seconds{goal="..."} 12.5
   looper_cost_dollars_total 0.02
   looper_llm_request_count{provider="openrouter", model="gpt-4o"} 15
   looper_test_pass_rate 0.87
   looper_build_success_total 42
   looper_build_failure_total 3
   ```
2. Gunakan `prometheus_client` (1-file, minimal) atau OTel metrics
3. Ekspor via HTTP di daemon (`--metrics-port 9100`)

### Dampak
- **Monitoring:** Grafana dashboard untuk build health
- **Alerting:** Prometheus alert rules untuk cost spikes, build failures

### Estimasi Usaha
- **Small (1-2 hari dev)** — tambah prometheus_client + exporter

---

## 8. Multi-Language Adapters

### Gap Saat Ini
`looper/languages.py` hanya support **Python**-specific adapters:
- `lint`: `ruff check`
- `test`: `python -m pytest`
- `syntax`: `python -m py_compile`

### Rekomendasi Teknis
1. Definisikan `LanguageAdapter` ABC:
   ```python
   class LanguageAdapter(ABC):
       @abstractmethod
       def lint(self, path: Path) -> LintResult: ...
       @abstractmethod
       def test(self, path: Path) -> TestResult: ...
       @abstractmethod
       def syntax_check(self, path: Path) -> bool: ...
   ```
2. Implement adapter untuk:
   - TypeScript: `npx eslint`, `npx tsc --noEmit`
   - Go: `golangci-lint`, `go test`
   - Rust: `cargo clippy`, `cargo test`
3. Auto-detection via file extension atau `pyproject.toml` / `package.json` / `go.mod`

### Dampak
- **Market reach:** bisa build TypeScript/Go/Rust project, bukan hanya Python
- **Versatility:** satu tool untuk seluruh stack

### Estimasi Usaha
- **Large (4-5 hari dev)** — implementasi masing-masing adapter + integration testing

---

## 9. Distributed Build Lock

### Gap Saat Ini
`looper/orchestrator.py` menggunakan `asyncio.Lock()` yang **in-memory**. Ini tidak work di multi-instance deployment (misal: load balancer dengan 2 daemon Looper).

### Bukti
```
looper/orchestrator.py:52:    self._build_lock: asyncio.Lock = asyncio.Lock()
```

### Rekomendasi Teknis
1. Ganti dengan Redis-based lock:
   ```python
   import aioredis
   lock = RedLock(f"looper:build:{goal_hash}", redis=redis_client, ttl=300)
   async with lock:
       await self._run_build()
   ```
2. Atau gunakan file-based lock (cross-process, cross-machine via NFS):
   ```python
   import portalocker
   with portalocker.Lock("looper_build.lock", timeout=1):
       await self._run_build()
   ```
3. Fallback: jika Redis tidak tersedia, gunakan advisory lock (graceful degradation)

### Dampak
- **Scalability:** beberapa daemon Looper dapat berkoordinasi
- **HA:** failover otomatis ketika satu daemon mati

### Estimati Usaha
- **Medium (2-3 hari dev)** — tambah redis client + lock logic + fallback mechanism

---

## 10. Artifact Registry (Distribusi Artifacts)

### Gap Saat Ini
Generated artifacts (code + tests) hanya tersedia di local filesystem. Untuk tim/project lain, butuh copy manual.

### Rekomendasi Teknis
1. Tambahkan upload capability:
   - S3 / GCS / Azure Blob (configurable via `storage.backend`)
   - Upload setiap cycle's artifacts as tarball
2. Metadata registry:
   ```json
   {"goal": "...", "cycle": 5, "score": 69.4, "artifacts": ["src.zip", "tests.zip"]}
   ```
3. CLI: `looper artifacts push`, `looper artifacts pull <hash>`

### Dampak
- **Sharing:** tim bisa mereuse artifacts dari build sebelumnya
- **Audit trail:** semua output tercatat dan dapat diakses

### Estimasi Usaha
- **Medium (3-4 hari dev)** — SDK storage + CLI + registry logic

---

## 30-Hari Action Plan (Per Prioritas)

### Minggu 1: Bugfix + Core Abstraction
- [ ] **Day 1-2:** LLM Provider Abstraction (Gap #1) — refactor OpenRouterClient → ABC pattern
- [ ] **Day 3-4:** Structured output untuk scoring (Gap #2) — update reviewer + security prompts
- [ ] **Day 5-7:** Test integration + verification (pytest, mypy, flake8)

### Minggu 2: Observability
- [ ] **Day 8-10:** OpenTelemetry integration (Gap #6) — add tracing + metrics
- [ ] **Day 11-12:** Prometheus metrics endpoint (Gap #7)
- [ ] **Day 13-14:** Webhook retry + cost alerting (Gap #3, Gap #4)

### Minggu 3: Extensibility
- [ ] **Day 15-18:** Phase plugin system (Gap #5) — registry + entry-point discovery
- [ ] **Day 19-21:** Multi-language adapters — TypeScript adapter MVP (Gap #8)
- [ ] **Day 22-21:** Update ADRs (Architecture Decision Records) untuk semua perubahan

### Minggu 4: Enterprise Readiness
- [ ] **Day 22-24:** Distributed build lock (Gap #9) — Redis fallback
- [ ] **Day 25-27:** Artifact registry (Gap #10) — S3 upload MVP
- [ ] **Day 28-30:** Integration testing penuh + release preparation

### Metrics for Success (30 Hari)
| Metric | Target |
|---|---|
| Test coverage | **100% line + branch** |
| mypy --strict | **0 error** |
| flake8 | **0 issue** |
| bandit | **0 issue** |
| black | **0 reformat** |
| Provider support | **2** providers (OpenRouter + OpenAI) |
| External phase | **1** terdaftar via entry point |
| Cost alerting | **80%/90%** threshold notification |
| Webhook retry | **3** retry + exponential backoff |
| Multi-language | **TypeScript** adapter viable |
| OTEL tracing | **Span per phase + LLM call** |
| Release | **v2.2.0** tagged |

---

## Prioritas Berdasarkan Dampak/Benefit Ratio

| Gap | Impact (User) | Effort | Dampak/Benefit | Rekomendasi |
|---|---|---|---|---|
| #6 Windows Bug (sudah selesai) | ⭐⭐⭐⭐⭐ | — | 🔥 **URGENT (sudah diperbaiki)** | DONE — di-deploy v2.2.1 |
| #1 LLM Provider | ⭐⭐⭐⭐ | Medium | 🔥 High | Minggu 1 |
| #3 Webhook Retry | ⭐⭐⭐⭐ | Medium | 🔥 High | Minggu 2 |
| #4 Cost Alerting | ⭐⭐⭐⭐ | Small | 🔥 High | Minggu 2 |
| #2 Structured Output | ⭐⭐⭐ | Small | ⭐⭐ High | Minggu 1 |
| #6 OTEL | ⭐⭐⭐ | Medium | ⭐⭐ Medium | Minggu 2 |
| #5 Plugin System | ⭐⭐⭐ | Large | ⭐ Medium | Minggu 3 |
| #8 Multi-Language | ⭐⭐ | Large | ⭐ Medium | Minggu 3 |
| #7 Prometheus | ⭐⭐ | Small | ⭐ Medium | Minggu 2 |
| #9 Dist Lock | — | Medium | ⭐ Low | Minggu 4 |
| #10 Artifact Reg | — | Medium | ⭐ Low | Minggu 4 |

> **Catatan:** Gap #9 dan #10 tidak memberi nilai hingga Looper digunakan dalam deployment multi-instance. Fokuskan effort pada Gap #1-#4 dulu untuk maksimalkan user impact.