// Same toolchain as the TV app (ctgmovies-tv/tv-app): AGP 8.7.3, Kotlin 2.0.21, compileSdk 35.
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.0.21" apply false
    // Chaquopy 17.2: Python 3.10–3.15 on AGP 7.3–9.5, minSdk 24
    id("com.chaquo.python") version "17.2.0" apply false
}
