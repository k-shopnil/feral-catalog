package app.feral.catalog

import android.content.Context
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import java.io.File

/** Paths and the Python module (catalog/mobile.py, bundled from the repo at build time). */
object Engine {
    /** FERAL_HOME: data/state.db, site/, logs/, reports/ */
    fun home(ctx: Context): File = File(ctx.filesDir, "feral").apply { mkdirs() }

    fun logsDir(ctx: Context): File = File(home(ctx), "logs")

    /** FERAL_CONFIG: the repo's config YAML files as bundled in this APK, refreshed before every use. */
    fun configDir(ctx: Context): File {
        val dir = File(ctx.filesDir, "config").apply { mkdirs() }
        val names = ctx.assets.list("config").orEmpty()
        for (name in names) {
            ctx.assets.open("config/$name").use { input -> File(dir, name).outputStream().use { input.copyTo(it) } }
        }
        dir.listFiles()?.filter { it.name !in names }?.forEach { it.delete() }
        return dir
    }

    fun module(): PyObject = Python.getInstance().getModule("catalog.mobile")
}
