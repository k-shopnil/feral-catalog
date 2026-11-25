package app.feral.catalog

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Build
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        if (Build.VERSION.SDK_INT >= 26) {
            getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(RunService.CHANNEL, "Catalog runs", NotificationManager.IMPORTANCE_LOW),
            )
        }
    }
}
