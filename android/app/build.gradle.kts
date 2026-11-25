import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.chaquo.python")
}

// The phone runs the repo's own catalog package and config (no copy to keep in sync by hand): both are copied
// from the repo root into build/generated on every build.
val repoRoot = rootDir.parentFile
val genPython = layout.buildDirectory.dir("generated/catalogPython")
val genAssets = layout.buildDirectory.dir("generated/catalogAssets")

// Owner's tokens, baked into this private sideload build (owner request, 2026-11-25). They come from
// android/secrets.properties, which is git-ignored: this repo is public, so they must never be committed.
val secrets = Properties().apply {
    val f = rootDir.resolve("secrets.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
fun secret(key: String): String = (secrets.getProperty(key) ?: "").trim().replace("\\", "\\\\").replace("\"", "\\\"")

val syncCatalogPython by tasks.registering(Sync::class) {
    from(repoRoot.resolve("catalog")) { include("**/*.py") }
    into(genPython.map { it.dir("catalog") })
}
val syncCatalogConfig by tasks.registering(Sync::class) {
    from(repoRoot.resolve("config")) { include("*.yaml") }
    into(genAssets.map { it.dir("config") })
}

android {
    namespace = "app.feral.catalog"
    compileSdk = 35

    defaultConfig {
        applicationId = "app.feral.catalog"
        minSdk = 24
        targetSdk = 35
        versionCode = 3
        versionName = "0.3.0"
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
        buildConfigField("String", "TMDB_TOKEN", "\"${secret("TMDB_READ_ACCESS")}\"")
        buildConfigField("String", "GITHUB_TOKEN", "\"${secret("GITHUB_TOKEN")}\"")
        buildConfigField("String", "FANART_KEY", "\"${secret("FANART_API_KEY")}\"")
        buildConfigField("String", "GDRIVE_CLIENT_ID", "\"${secret("GDRIVE_CLIENT_ID")}\"")
        buildConfigField("String", "GDRIVE_CLIENT_SECRET", "\"${secret("GDRIVE_CLIENT_SECRET")}\"")
        buildConfigField("String", "GDRIVE_REFRESH_TOKEN", "\"${secret("GDRIVE_REFRESH_TOKEN")}\"")
    }

    buildTypes {
        release {
            // Chaquopy calls the Kotlin listener by method name, so keep the code unobfuscated.
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug") // sideload only, like the TV app's MVP builds
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    sourceSets["main"].assets.srcDir(genAssets)
}

chaquopy {
    defaultConfig {
        version = "3.12"
        pip {
            // Same versions as the PC indexer's .venv (pip freeze, 2026-11-25), except PyYAML: 6.0.3 is the newest
            // build Chaquopy provides for Android (PC: 6.0.4; safe_load behaves the same).
            install("httpx==0.28.1")
            install("httpcore==1.0.9")
            install("h11==0.16.0")
            install("anyio==4.16.0")
            install("idna==3.20")
            install("certifi==2026.11.20")
            install("typing_extensions==4.16.0")
            install("PyYAML==6.0.3")
            install("python-dotenv==1.2.4")
            install("guessit==4.5.5")
            install("rebulk==6.0.3")
            install("babelfish==0.6.1")
            install("python-dateutil==4.0.0")
        }
        // these read data files next to their modules (certifi's CA bundle, guessit/babelfish/rebulk data)
        extractPackages("certifi", "guessit", "babelfish", "rebulk")
    }
    sourceSets {
        getByName("main") {
            srcDir(genPython)
            // rapidfuzz 3.14.6 with its pure-Python implementation only (no Android build exists)
            srcDir("src/main/python-vendor")
        }
    }
}

tasks.named("preBuild") { dependsOn(syncCatalogPython, syncCatalogConfig) }
tasks.matching { it.name.contains("Python") && it.name != "syncCatalogPython" }.configureEach {
    dependsOn(syncCatalogPython)
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.12.01")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.security:security-crypto:1.0.0")
}
