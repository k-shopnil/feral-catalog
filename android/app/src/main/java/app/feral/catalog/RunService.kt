package app.feral.catalog

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject
import kotlin.concurrent.thread

/**
 * Runs the connection checks or a full catalog update in a foreground service, so a 20+ minute run keeps going
 * with the screen off. Holds a partial wake lock for the duration.
 */
class RunService : Service() {
    companion object {
        const val CHANNEL = "runs"
        private const val NOTE_ID = 1
        private const val DONE_ID = 2
        const val ACTION_CHECK = "app.feral.catalog.CHECK"
        const val ACTION_RUN = "app.feral.catalog.RUN"

        /** One-tap update: picks up whatever is new and publishes it (publish on, no force, no full re-crawl). */
        const val ACTION_QUICK = "app.feral.catalog.QUICK"

        @Volatile
        var busy = false
            private set

        fun start(ctx: Context, action: String) {
            ContextCompat.startForegroundService(ctx, Intent(ctx, RunService::class.java).setAction(action))
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action ?: return START_NOT_STICKY
        startInForeground(progressNote("Starting…", 0))
        if (busy) return START_NOT_STICKY
        busy = true
        thread(name = "catalog-run") { work(action) }
        return START_NOT_STICKY
    }

    private fun work(action: String) {
        val quick = action == ACTION_QUICK
        val isRun = action == ACTION_RUN || quick
        val prefs = Prefs(this)
        val wake = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "feral:catalog").apply { acquire(3 * 60 * 60 * 1000L) }
        val started = System.currentTimeMillis()
        RunState.begin(if (isRun) Mode.RUNNING else Mode.CHECKING)
        RunState.note("network: ${Net.describe(this)}")
        if (quick) RunState.note("quick update: publish on, force off, full re-crawl off")
        val ticker = thread(name = "catalog-notify", isDaemon = true) {
            try {
                while (true) {
                    Thread.sleep(2000)
                    val s = RunState.state.value
                    val cur = s.stages.lastOrNull { it.status == Status.RUNNING }
                    val text = cur?.let { if (it.detail.isNotEmpty()) "${it.label}: ${it.detail}" else it.label } ?: "Working…"
                    notify(NOTE_ID, progressNote(text, (RunState.overallProgress(s) * 100).toInt()))
                }
            } catch (_: InterruptedException) {
                // run finished
            }
        }
        val outcome = try {
            val home = Engine.home(this).path
            val cfg = Engine.configDir(this).path
            val py = Engine.module()
            val listener = RunState.PyListener()
            if (isRun) {
                val res = JSONObject(
                    py.callAttr("run", listener, home, cfg, prefs.tmdbToken, prefs.githubToken,
                        quick || prefs.publish, !quick && prefs.fullCrawl, !quick && prefs.forcePublish, BuildConfig.FANART_KEY, driveJson()).toString(),
                )
                Outcome("run", res.optBoolean("ok"), res.optString("message"), res.optString("log_file").ifEmpty { null },
                    started, System.currentTimeMillis())
            } else {
                val ok = py.callAttr("checks", listener, home, cfg, prefs.tmdbToken, prefs.githubToken, prefs.publish, BuildConfig.FANART_KEY, driveJson()).toBoolean()
                val msg = if (ok) "All checks passed: this network can run a catalog update."
                else "Some checks failed: see the rows marked FAILED."
                Outcome("check", ok, msg, null, started, System.currentTimeMillis())
            }
        } catch (e: Throwable) {
            // Python errors are already handled in mobile.py; this is the app side failing (e.g. Python not starting).
            Log.e("feral", "run failed", e)
            RunState.note("app error: ${e.stackTraceToString()}")
            Outcome(if (isRun) "run" else "check", false, "App error: ${e.message ?: e.javaClass.simpleName}", null,
                started, System.currentTimeMillis())
        }
        RunState.finish(outcome)
        ticker.interrupt()
        if (wake.isHeld) wake.release()
        busy = false
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        if (isRun) {
            notify(DONE_ID, NotificationCompat.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_launcher)
                .setContentTitle(if (outcome.ok) "Catalog update: SUCCESS" else "Catalog update: FAILURE")
                .setContentText(outcome.message)
                .setStyle(NotificationCompat.BigTextStyle().bigText(outcome.message))
                .setContentIntent(openApp())
                .setAutoCancel(true)
                .build())
        }
        stopSelf()
    }

    /** Google Drive state hand-off credentials (baked in from secrets.properties); "" turns the hand-off off. */
    private fun driveJson(): String =
        if (BuildConfig.GDRIVE_REFRESH_TOKEN.isBlank()) ""
        else JSONObject()
            .put("client_id", BuildConfig.GDRIVE_CLIENT_ID)
            .put("client_secret", BuildConfig.GDRIVE_CLIENT_SECRET)
            .put("refresh_token", BuildConfig.GDRIVE_REFRESH_TOKEN)
            .toString()

    private fun startInForeground(n: Notification) {
        val type = if (Build.VERSION.SDK_INT >= 29) ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC else 0
        ServiceCompat.startForeground(this, NOTE_ID, n, type)
    }

    private fun progressNote(text: String, pct: Int): Notification =
        NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle("feral catalog: $pct%")
            .setContentText(text)
            .setProgress(100, pct, false)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(openApp())
            .build()

    private fun openApp(): PendingIntent = PendingIntent.getActivity(
        this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
    )

    private fun notify(id: Int, n: Notification) {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT < 33 ||
            ContextCompat.checkSelfPermission(this, android.Manifest.permission.POST_NOTIFICATIONS) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED
        ) nm.notify(id, n)
    }
}
