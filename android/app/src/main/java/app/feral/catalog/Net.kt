package app.feral.catalog

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities

object Net {
    /** "Wi-Fi · internet OK", "Mobile data · VPN · no internet", "No network". */
    fun describe(ctx: Context): String {
        val cm = ctx.getSystemService(ConnectivityManager::class.java)
        val net = cm.activeNetwork ?: return "No network"
        val caps = cm.getNetworkCapabilities(net) ?: return "No network"
        val kinds = buildList {
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) add("Wi-Fi")
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) add("Mobile data")
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) add("Ethernet")
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) add("VPN")
        }.ifEmpty { listOf("Other") }
        val internet = if (caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)) "internet OK" else "no internet"
        return (kinds + internet).joinToString(" · ")
    }
}
