package app.feral.catalog

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKeys

/**
 * Tokens: whatever is saved in Setup (encrypted with an Android Keystore key) wins; otherwise the tokens baked
 * into this build from android/secrets.properties are used.
 */
class Prefs(ctx: Context) {
    private val sp: SharedPreferences = EncryptedSharedPreferences.create(
        "feral_catalog_secrets",
        MasterKeys.getOrCreate(MasterKeys.AES256_GCM_SPEC),
        ctx.applicationContext,
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    var tmdbToken: String
        get() = sp.getString("tmdb", "").orEmpty().ifEmpty { BuildConfig.TMDB_TOKEN }
        set(v) = sp.edit().putString("tmdb", v.trim()).apply()

    var githubToken: String
        get() = sp.getString("github", "").orEmpty().ifEmpty { BuildConfig.GITHUB_TOKEN }
        set(v) = sp.edit().putString("github", v.trim()).apply()

    /** Only what was typed into Setup (blank = use the built-in token). */
    val tmdbOverride: String get() = sp.getString("tmdb", "").orEmpty()
    val githubOverride: String get() = sp.getString("github", "").orEmpty()

    /** Off = dry run: everything except the GitHub publish. */
    var publish: Boolean
        get() = sp.getBoolean("publish", true)
        set(v) = sp.edit().putBoolean("publish", v).apply()

    /** Publish even when nothing changed (re-uploads the current version). */
    var forcePublish: Boolean
        get() = sp.getBoolean("force_publish", false)
        set(v) = sp.edit().putBoolean("force_publish", v).apply()

    /** Re-list every folder instead of only changed/cold ones (slow). */
    var fullCrawl: Boolean
        get() = sp.getBoolean("full_crawl", false)
        set(v) = sp.edit().putBoolean("full_crawl", v).apply()
}
