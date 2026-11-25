package app.feral.catalog

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

enum class Mode { IDLE, CHECKING, RUNNING, FINISHED }
enum class Status { WAITING, RUNNING, DONE, FAILED, SKIPPED }

data class CheckRow(val id: String, val label: String, val status: String, val detail: String)

data class StageRow(
    val id: String,
    val label: String,
    val status: Status = Status.WAITING,
    val detail: String = "",
    val done: Int = 0,
    val total: Int = 0,
    val startedAt: Long = 0,
    val endedAt: Long = 0,
)

data class Outcome(
    val kind: String, // "check" | "run"
    val ok: Boolean,
    val message: String,
    val logFile: String?,
    val startedAt: Long,
    val finishedAt: Long,
)

data class UiState(
    val mode: Mode = Mode.IDLE,
    val checks: List<CheckRow> = emptyList(),
    val stages: List<StageRow> = RunState.freshStages(),
    val log: List<String> = emptyList(),
    val outcome: Outcome? = null,
    val startedAt: Long = 0,
)

/** Shared between the service (writer) and the screen (reader). Python calls [PyListener] from its own threads. */
object RunState {
    private val stageDefs = listOf(
        "checks" to "Connection checks",
        "handoff" to "Get latest state from Drive",
        "sync" to "Sync published catalog",
        "crawl" to "Crawl storage server",
        "process" to "Parse & group files",
        "match" to "Match titles on TMDB",
        "enrich" to "Fetch TMDB details",
        "publish" to "Build catalog files",
        "push" to "Publish to GitHub Pages",
        "upload" to "Save state to Drive",
    )

    /** Share of the overall bar per stage (crawl dominates a normal run). */
    private val weights = mapOf(
        "checks" to 3f, "handoff" to 4f, "upload" to 4f, "sync" to 4f, "crawl" to 45f, "process" to 8f,
        "match" to 15f, "enrich" to 12f, "publish" to 8f, "push" to 5f,
    )

    private const val MAX_LOG = 600

    fun freshStages() = stageDefs.map { (id, label) -> StageRow(id, label) }

    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state

    fun begin(mode: Mode) = _state.update {
        UiState(mode = mode, startedAt = System.currentTimeMillis(), log = listOf(stamp("INFO", "started (${mode.name.lowercase()})")))
    }

    fun finish(outcome: Outcome) = _state.update { s ->
        // anything still marked running when the run ends did not complete
        val stages = s.stages.map { if (it.status == Status.RUNNING) it.copy(status = Status.FAILED, endedAt = outcome.finishedAt) else it }
        s.copy(mode = Mode.FINISHED, outcome = outcome, stages = stages)
    }

    fun note(line: String) = appendLog(stamp("INFO", line))

    fun overallProgress(s: UiState): Float {
        val total = weights.values.sum()
        var got = 0f
        for (st in s.stages) {
            val w = weights[st.id] ?: 0f
            got += when (st.status) {
                Status.DONE, Status.SKIPPED -> w
                Status.RUNNING -> if (st.total > 0) w * (st.done.toFloat() / st.total).coerceIn(0f, 0.98f) else w * 0.05f
                else -> 0f
            }
        }
        return (got / total).coerceIn(0f, 1f)
    }

    private fun appendLog(line: String) = _state.update { s ->
        val log = if (s.log.size >= MAX_LOG) s.log.drop(s.log.size - MAX_LOG + 1) + line else s.log + line
        s.copy(log = log)
    }

    private fun stamp(level: String, msg: String): String =
        "${SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())} $level app: $msg"

    private fun updateStage(id: String, f: (StageRow) -> StageRow) = _state.update { s ->
        s.copy(stages = s.stages.map { if (it.id == id) f(it) else it })
    }

    /** Called by catalog/mobile.py through Chaquopy; the method names and signatures are part of that contract. */
    class PyListener {
        fun onCheck(id: String, label: String, status: String, detail: String) = _state.update { s ->
            s.copy(checks = s.checks.filter { it.id != id } + CheckRow(id, label, status, detail))
        }

        fun onStage(id: String, state: String, detailJson: String) {
            val now = System.currentTimeMillis()
            val detail = summarize(detailJson)
            updateStage(id) {
                when (state) {
                    "start" -> it.copy(status = Status.RUNNING, startedAt = now, detail = "", done = 0, total = 0)
                    "done" -> it.copy(status = Status.DONE, endedAt = now, detail = detail.ifEmpty { it.detail })
                    "skipped" -> it.copy(status = Status.SKIPPED, endedAt = now, detail = detail)
                    else -> it.copy(status = Status.FAILED, endedAt = now, detail = detail.ifEmpty { it.detail })
                }
            }
        }

        fun onProgress(id: String, done: Int, total: Int) = updateStage(id) {
            it.copy(done = done, total = total, detail = progressText(id, done, total))
        }

        fun onLog(level: String, line: String) = appendLog(line)
    }

    private fun progressText(id: String, done: Int, total: Int): String = when (id) {
        "crawl" -> "%,d folders listed, %,d queued".format(done, (total - done).coerceAtLeast(0))
        "match" -> "%,d of %,d titles matched".format(done, total)
        "enrich" -> "%,d of %,d titles fetched".format(done, total)
        "handoff" -> "downloaded %.1f of %.1f MB".format(done / 1024f, total / 1024f)
        "upload" -> "uploaded %.1f of %.1f MB".format(done / 1024f, total / 1024f)
        else -> "%,d / %,d".format(done, total)
    }

    /** {"listed": 12, "failed": 0, "nested": {...}} -> "listed 12 · failed 0" (top-level scalars only). */
    private fun summarize(json: String): String = runCatching {
        val o = JSONObject(json)
        o.keys().asSequence().mapNotNull { k ->
            when (val v = o.get(k)) {
                is JSONObject, is org.json.JSONArray -> null
                JSONObject.NULL -> null
                else -> "${k.replace('_', ' ')} $v"
            }
        }.joinToString(" · ")
    }.getOrDefault("")
}
