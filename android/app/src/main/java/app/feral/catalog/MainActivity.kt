package app.feral.catalog

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.text.format.Formatter
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.FileProvider
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.text.DateFormat
import java.util.Date

private val Ok = Color(0xFF4CD38A)
private val Bad = Color(0xFFFF6B6B)
private val Warn = Color(0xFFFFC857)
private val Dim = Color(0xFF9AA59F)
private val Mint = Color(0xFF73FFAA)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(primary = Mint, onPrimary = Color.Black, background = Color(0xFF0E1110),
                surface = Color(0xFF0E1110), surfaceVariant = Color(0xFF1A1F1C))) {
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) { Screen() }
            }
        }
    }
}

@Composable
private fun Screen() {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    val scope = rememberCoroutineScope()
    val state by RunState.state.collectAsStateWithLifecycle()
    val busy = state.mode == Mode.CHECKING || state.mode == Mode.RUNNING

    var network by remember { mutableStateOf(Net.describe(ctx)) }
    var stateInfo by remember { mutableStateOf<JSONObject?>(null) }
    var stateMsg by remember { mutableStateOf("") }
    var fileBusy by remember { mutableStateOf(false) }
    var showLog by remember { mutableStateOf<File?>(null) }

    val notifPerm = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
    LaunchedEffect(Unit) {
        if (Build.VERSION.SDK_INT >= 33) notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    // live network label
    DisposableEffect(Unit) {
        val cm = ctx.getSystemService(ConnectivityManager::class.java)
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) { refreshNet() }
            override fun onLost(network: Network) { refreshNet() }
            override fun onCapabilitiesChanged(n: Network, c: NetworkCapabilities) { refreshNet() }
            fun refreshNet() { network = Net.describe(ctx) }
        }
        cm.registerDefaultNetworkCallback(cb)
        onDispose { cm.unregisterNetworkCallback(cb) }
    }

    suspend fun loadStateInfo() {
        val r = withContext(Dispatchers.IO) {
            runCatching {
                JSONObject(Engine.module().callAttr("state_info", Engine.home(ctx).path, Engine.configDir(ctx).path).toString())
            }
        }
        r.onSuccess { stateInfo = it }.onFailure { stateInfo = null; stateMsg = "Couldn't read state: ${it.message}" }
    }
    LaunchedEffect(state.mode) { if (!busy) loadStateInfo() }

    val importLauncher = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        if (uri == null) return@rememberLauncherForActivityResult
        scope.launch {
            fileBusy = true
            stateMsg = "Importing state.db…"
            val r = withContext(Dispatchers.IO) {
                runCatching {
                    val tmp = File(ctx.cacheDir, "import-state.db")
                    ctx.contentResolver.openInputStream(uri)!!.use { input -> tmp.outputStream().use { input.copyTo(it) } }
                    try {
                        Engine.module().callAttr("import_state", Engine.home(ctx).path, Engine.configDir(ctx).path, tmp.path).toString()
                    } finally {
                        tmp.delete()
                    }
                }
            }
            r.onSuccess { backup -> stateMsg = "Imported." + if (backup.isNotEmpty()) " Previous state kept as ${File(backup).name}." else "" }
                .onFailure { stateMsg = "Import failed: ${it.message}" }
            loadStateInfo()
            fileBusy = false
        }
    }
    val exportLauncher = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("application/vnd.sqlite3")) { uri: Uri? ->
        if (uri == null) return@rememberLauncherForActivityResult
        scope.launch {
            fileBusy = true
            stateMsg = "Exporting state.db…"
            val r = withContext(Dispatchers.IO) {
                runCatching {
                    val tmp = File(ctx.cacheDir, "export-state.db").apply { delete() }
                    Engine.module().callAttr("export_state", Engine.home(ctx).path, Engine.configDir(ctx).path, tmp.path)
                    ctx.contentResolver.openOutputStream(uri, "w")!!.use { out -> tmp.inputStream().use { it.copyTo(out) } }
                    val size = tmp.length()
                    tmp.delete()
                    size
                }
            }
            stateMsg = r.fold({ "Exported ${Formatter.formatFileSize(ctx, it)}. Copy it to the PC and run: python -m catalog import-state --file <path>" },
                { "Export failed: ${it.message}" })
            fileBusy = false
        }
    }

    val listState = rememberLazyListState()
    // a run's live status sits in the Quick update card at the top: bring it into view when a run starts
    LaunchedEffect(state.mode) { if (state.mode == Mode.RUNNING) listState.animateScrollToItem(0) }

    LazyColumn(
        Modifier.fillMaxSize().safeDrawingPadding().imePadding(),
        state = listState,
        contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Text("feral catalog", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.SemiBold)
            Text("Runs the catalog update from this phone instead of the PC.", color = Dim, fontSize = 13.sp)
        }

        // ---- quick update (one tap; the advanced controls below stay as they are)
        item {
            val hasState = stateInfo?.optBoolean("exists") == true
            Card(colors = CardDefaults.cardColors(containerColor = Color(0xFF15261D)), modifier = Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Quick update", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                    Text("Checks the connection, finds anything new on the server and publishes it to the TVs.",
                        color = Dim, fontSize = 13.sp)
                    Button(
                        onClick = { RunService.start(ctx, RunService.ACTION_QUICK) },
                        enabled = !busy && !fileBusy && hasState,
                        modifier = Modifier.fillMaxWidth().height(56.dp),
                    ) {
                        Text(when (state.mode) {
                            Mode.RUNNING -> "Updating…"
                            Mode.CHECKING -> "Checking connection…"
                            else -> "Update now"
                        }, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
                    }
                    if (stateInfo != null && !hasState) {
                        Text("Import the catalog state first (Setup → Import state).", color = Warn, fontSize = 12.sp)
                    }
                    val o = state.outcome
                    if (state.mode == Mode.RUNNING) {
                        val pct = (RunState.overallProgress(state) * 100).toInt()
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            LinearProgressIndicator(progress = { pct / 100f }, modifier = Modifier.weight(1f).height(8.dp), color = Mint)
                            Spacer(Modifier.width(10.dp))
                            Text("$pct%", fontFamily = FontFamily.Monospace)
                        }
                        val cur = state.stages.lastOrNull { it.status == Status.RUNNING }
                        Text(cur?.let { "Now: ${it.label}" + if (it.detail.isNotEmpty()) " — ${it.detail}" else "" } ?: "Starting…",
                            fontSize = 13.sp)
                        Elapsed(state)
                    } else if (state.mode == Mode.FINISHED && o != null && o.kind == "run") {
                        Text(if (o.ok) "SUCCESS" else "FAILURE", color = if (o.ok) Ok else Bad, fontWeight = FontWeight.Bold, fontSize = 20.sp)
                        Text(o.message, fontSize = 13.sp)
                        Text("Finished ${fmtTime(o.finishedAt)} · took ${fmtDuration(o.finishedAt - o.startedAt)} · details below",
                            color = Dim, fontSize = 12.sp)
                    }
                }
            }
        }

        // ---- network
        item {
            Section("Network") {
                Label("Current connection", network)
                Text("The storage server answers only on BDIX networks. Run “Check connection” to test this one.",
                    color = Dim, fontSize = 12.sp)
            }
        }

        // ---- setup
        item {
            Section("Setup") {
                var tmdb by remember { mutableStateOf(prefs.tmdbOverride) }
                var gh by remember { mutableStateOf(prefs.githubOverride) }
                var publish by remember { mutableStateOf(prefs.publish) }
                var full by remember { mutableStateOf(prefs.fullCrawl) }
                var force by remember { mutableStateOf(prefs.forcePublish) }
                var saved by remember { mutableStateOf(false) }
                if (BuildConfig.TMDB_TOKEN.isNotEmpty() && BuildConfig.GITHUB_TOKEN.isNotEmpty()) {
                    Text("Tokens are built into this app. Fill these in only to override them.", color = Dim, fontSize = 12.sp)
                }
                SecretField("TMDB token (blank = built-in)", tmdb) { tmdb = it; saved = false }
                SecretField("GitHub token (blank = built-in)", gh) { gh = it; saved = false }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Button(onClick = { prefs.tmdbToken = tmdb; prefs.githubToken = gh; saved = true }, enabled = !busy) { Text("Save tokens") }
                    Spacer(Modifier.width(12.dp))
                    if (saved) Text("Saved (encrypted on this phone)", color = Ok, fontSize = 12.sp)
                }
                Toggle("Publish to GitHub", "Off = dry run: everything except the publish", publish, !busy) { publish = it; prefs.publish = it }
                Toggle("Force publish", "Publish even if nothing changed", force, !busy) { force = it; prefs.forcePublish = it }
                Toggle("Full re-crawl", "Re-list every folder (much slower). Normally off.", full, !busy) { full = it; prefs.fullCrawl = it }

                HorizontalDivider(Modifier.padding(vertical = 8.dp))
                Text("Catalog state (state.db)", fontWeight = FontWeight.Medium)
                val si = stateInfo
                when {
                    si == null -> Text(stateMsg.ifEmpty { "Loading…" }, color = Dim, fontSize = 13.sp)
                    !si.optBoolean("exists") -> Text("No state yet. Import the PC's state.db (on the PC: python -m catalog export-state --file state.db).",
                        color = Warn, fontSize = 13.sp)
                    else -> {
                        Label("Catalog version", si.optInt("version").toString() + "  (published from here: ${si.optString("pushed_version", "-")})")
                        Label("Size", Formatter.formatFileSize(ctx, si.optLong("bytes")) + " · %,d files tracked".format(si.optInt("files")))
                        Label("Last run finished", si.optString("last_run_finished_at").toLongOrNull()?.let { fmtTime(it * 1000) } ?: "never")
                    }
                }
                if (si != null && stateMsg.isNotEmpty() && !stateMsg.startsWith("Couldn")) Text(stateMsg, color = Dim, fontSize = 12.sp)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { importLauncher.launch(arrayOf("*/*")) }, enabled = !busy && !fileBusy) { Text("Import state…") }
                    OutlinedButton(onClick = { exportLauncher.launch("state.db") },
                        enabled = !busy && !fileBusy && stateInfo?.optBoolean("exists") == true) { Text("Export state…") }
                }
            }
        }

        // ---- checks
        item {
            Section("Connection check") {
                Button(onClick = { RunService.start(ctx, RunService.ACTION_CHECK) }, enabled = !busy && !fileBusy) {
                    Text(if (state.mode == Mode.CHECKING) "Checking…" else "Check connection")
                }
                if (state.checks.isEmpty()) {
                    Text("Tests the storage server, an indexed folder, TMDB, the published catalog, GitHub access and the local state over this network.",
                        color = Dim, fontSize = 12.sp)
                }
                state.checks.forEach { c -> CheckLine(c) }
            }
        }

        // ---- run
        item {
            Section("Catalog update") {
                Button(onClick = { RunService.start(ctx, RunService.ACTION_RUN) },
                    enabled = !busy && !fileBusy && stateInfo?.optBoolean("exists") == true) {
                    Text(if (state.mode == Mode.RUNNING) "Running…" else "Run update")
                }
                val showRun = state.mode == Mode.RUNNING || (state.mode == Mode.FINISHED && state.outcome?.kind == "run")
                if (showRun) {
                    val p = RunState.overallProgress(state)
                    val pct = if (state.mode == Mode.FINISHED && state.outcome?.ok == true) 100 else (p * 100).toInt()
                    Spacer(Modifier.height(4.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        LinearProgressIndicator(progress = { pct / 100f }, modifier = Modifier.weight(1f).height(8.dp),
                            color = if (state.outcome?.ok == false) Bad else Mint)
                        Spacer(Modifier.width(10.dp))
                        Text("$pct%", fontFamily = FontFamily.Monospace)
                    }
                    val cur = state.stages.lastOrNull { it.status == Status.RUNNING }
                    if (cur != null) Text("Now: ${cur.label}" + if (cur.detail.isNotEmpty()) " — ${cur.detail}" else "", fontSize = 13.sp)
                    Elapsed(state)
                    Spacer(Modifier.height(4.dp))
                    state.stages.forEach { StageLine(it) }
                } else {
                    Text("Same pipeline as the PC: checks → sync → crawl → parse → match → details → build → publish.",
                        color = Dim, fontSize = 12.sp)
                }
            }
        }

        // ---- result
        state.outcome?.let { o ->
            item {
                Card(colors = CardDefaults.cardColors(containerColor = if (o.ok) Color(0xFF12321F) else Color(0xFF3A1414)),
                    modifier = Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        val what = if (o.kind == "run") "Catalog update" else "Connection check"
                        Text("$what: ${if (o.ok) "SUCCESS" else "FAILURE"}", color = if (o.ok) Ok else Bad,
                            fontWeight = FontWeight.Bold, fontSize = 20.sp)
                        Text(o.message)
                        Text("Finished ${fmtTime(o.finishedAt)} · took ${fmtDuration(o.finishedAt - o.startedAt)}", color = Dim, fontSize = 12.sp)
                        val lf = o.logFile?.let(::File)?.takeIf { it.exists() }
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            if (lf != null) {
                                OutlinedButton(onClick = { showLog = lf }) { Text("Full log") }
                                OutlinedButton(onClick = { shareLog(ctx, lf) }) { Text("Share log") }
                            }
                            OutlinedButton(onClick = { copyText(ctx, state.log.joinToString("\n")) }) { Text("Copy log") }
                        }
                    }
                }
            }
        }

        // ---- live log
        if (state.log.isNotEmpty()) {
            item {
                Section("Detailed log (latest ${state.log.size} lines)") {
                    LogBox(state.log)
                }
            }
        }
    }

    showLog?.let { f -> FullLogDialog(f) { showLog = null } }
}

@Composable
private fun Section(title: String, content: @Composable () -> Unit) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            content()
        }
    }
}

@Composable
private fun Label(name: String, value: String) {
    Row {
        Text("$name: ", color = Dim, fontSize = 13.sp)
        Text(value, fontSize = 13.sp)
    }
}

@Composable
private fun SecretField(label: String, value: String, onChange: (String) -> Unit) {
    var show by remember { mutableStateOf(false) }
    OutlinedTextField(
        value = value, onValueChange = onChange, label = { Text(label) }, singleLine = true,
        visualTransformation = if (show) VisualTransformation.None else PasswordVisualTransformation(),
        trailingIcon = { TextButton(onClick = { show = !show }) { Text(if (show) "Hide" else "Show") } },
        modifier = Modifier.fillMaxWidth(),
    )
}

@Composable
private fun Toggle(title: String, sub: String, value: Boolean, enabled: Boolean, onChange: (Boolean) -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.weight(1f)) {
            Text(title)
            Text(sub, color = Dim, fontSize = 12.sp)
        }
        Switch(checked = value, onCheckedChange = onChange, enabled = enabled)
    }
}

@Composable
private fun Badge(text: String, color: Color) {
    Box(Modifier.background(color.copy(alpha = 0.18f), RoundedCornerShape(6.dp)).padding(horizontal = 6.dp, vertical = 2.dp)) {
        Text(text, color = color, fontSize = 11.sp, fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace)
    }
}

@Composable
private fun CheckLine(c: CheckRow) {
    Row(verticalAlignment = Alignment.Top) {
        when (c.status) {
            "ok" -> Badge("  OK  ", Ok)
            "skip" -> Badge(" SKIP ", Dim)
            else -> Badge("FAILED", Bad)
        }
        Spacer(Modifier.width(8.dp))
        Column {
            Text(c.label, fontWeight = FontWeight.Medium, fontSize = 14.sp)
            Text(c.detail, color = Dim, fontSize = 12.sp)
        }
    }
}

@Composable
private fun StageLine(s: StageRow) {
    Row(verticalAlignment = Alignment.Top, modifier = Modifier.padding(vertical = 2.dp)) {
        when (s.status) {
            Status.WAITING -> Badge("WAIT", Dim)
            Status.RUNNING -> Badge(" RUN", Warn)
            Status.DONE -> Badge("DONE", Ok)
            Status.SKIPPED -> Badge("SKIP", Dim)
            Status.FAILED -> Badge("FAIL", Bad)
        }
        Spacer(Modifier.width(8.dp))
        Column(Modifier.weight(1f)) {
            Row {
                Text(s.label, fontSize = 14.sp, fontWeight = if (s.status == Status.RUNNING) FontWeight.SemiBold else FontWeight.Normal,
                    modifier = Modifier.weight(1f))
                if (s.startedAt > 0) {
                    val end = if (s.endedAt > 0) s.endedAt else System.currentTimeMillis()
                    Text(fmtDuration(end - s.startedAt), color = Dim, fontSize = 12.sp, fontFamily = FontFamily.Monospace)
                }
            }
            if (s.detail.isNotEmpty()) Text(s.detail, color = Dim, fontSize = 12.sp)
            if (s.status == Status.RUNNING && s.total > 0) {
                LinearProgressIndicator(progress = { (s.done.toFloat() / s.total).coerceIn(0f, 1f) },
                    modifier = Modifier.fillMaxWidth().padding(top = 3.dp).height(3.dp), color = Warn)
            }
        }
    }
}

@Composable
private fun Elapsed(state: UiState) {
    var now by remember { mutableStateOf(System.currentTimeMillis()) }
    LaunchedEffect(state.mode) {
        while (state.mode == Mode.RUNNING) { now = System.currentTimeMillis(); delay(1000) }
    }
    val end = state.outcome?.finishedAt ?: now
    Text("Elapsed ${fmtDuration(end - state.startedAt)}", color = Dim, fontSize = 12.sp)
}

@Composable
private fun LogBox(lines: List<String>) {
    val list = rememberLazyListState()
    LaunchedEffect(lines.size) { if (lines.isNotEmpty()) list.scrollToItem(lines.size - 1) }
    Box(Modifier.fillMaxWidth().height(320.dp).background(Color.Black, RoundedCornerShape(8.dp)).padding(8.dp)) {
        SelectionContainer {
            LazyColumn(state = list, modifier = Modifier.horizontalScroll(rememberScrollState())) {
                items(lines) { l ->
                    Text(l, fontFamily = FontFamily.Monospace, fontSize = 10.sp, softWrap = false, color = when {
                        " ERROR " in l -> Bad
                        " WARNING " in l -> Warn
                        else -> Color(0xFFCFD8D3)
                    })
                }
            }
        }
    }
}

@Composable
private fun FullLogDialog(f: File, onDismiss: () -> Unit) {
    var lines by remember { mutableStateOf<List<String>?>(null) }
    LaunchedEffect(f) { lines = withContext(Dispatchers.IO) { runCatching { f.readLines() }.getOrElse { listOf("Can't read ${f.name}: ${it.message}") } } }
    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = { TextButton(onClick = onDismiss) { Text("Close") } },
        title = { Text(f.name, fontSize = 14.sp) },
        text = { lines?.let { LogBox(it) } ?: Text("Loading…") },
    )
}

private fun shareLog(ctx: android.content.Context, f: File) {
    val uri = FileProvider.getUriForFile(ctx, "app.feral.catalog.files", f)
    ctx.startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).apply {
        type = "text/plain"
        putExtra(Intent.EXTRA_STREAM, uri)
        putExtra(Intent.EXTRA_SUBJECT, "feral catalog ${f.name}")
        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    }, "Share log"))
}

private fun copyText(ctx: android.content.Context, text: String) {
    ctx.getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("feral catalog log", text))
}

private fun fmtTime(ms: Long): String = DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT).format(Date(ms))

private fun fmtDuration(ms: Long): String {
    val s = (ms / 1000).coerceAtLeast(0)
    return if (s >= 3600) "%dh %02dm".format(s / 3600, (s % 3600) / 60) else "%dm %02ds".format(s / 60, s % 60)
}
