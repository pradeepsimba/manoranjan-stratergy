plugins {
    application
    id("org.openjfx.javafxplugin") version "0.1.0"
    id("org.beryx.runtime") version "2.0.1"
}

repositories {
    mavenCentral()
}

dependencies {
    implementation("com.fasterxml.jackson.core:jackson-databind:2.16.1")
    implementation("org.xerial:sqlite-jdbc:3.44.1.0")
}

java {
    toolchain {
        languageVersion = JavaLanguageVersion.of(26)
    }
}

javafx {
    version = "26"
    modules = listOf("javafx.controls", "javafx.fxml")
}

application {
    mainClass = "org.example.hellofx.Launcher"
    applicationName = "hellofx"
    applicationDefaultJvmArgs = listOf("--enable-native-access=ALL-UNNAMED")
}

runtime {
    options = mutableListOf("--strip-debug", "--compress=zip-6", "--no-header-files", "--no-man-pages")

    launcher {
        noConsole = true
    }
    jpackage {
        val currentOs = org.gradle.internal.os.OperatingSystem.current()
        val imgType = if (currentOs.isWindows) "ico" else if (currentOs.isMacOsX) "icns" else "png"
        imageOptions.addAll(listOf("--icon", layout.projectDirectory.file("src/main/resources/hellofx.$imgType").asFile.absolutePath))
        installerOptions.addAll(listOf("--resource-dir", layout.projectDirectory.dir("src/main/resources").asFile.absolutePath))
        installerOptions.addAll(listOf("--vendor", "Acme Corporation"))

        if (currentOs.isWindows) {
            installerOptions.addAll(listOf("--win-per-user-install", "--win-dir-chooser", "--win-menu", "--win-shortcut"))
        } else if (currentOs.isLinux) {
            installerOptions.addAll(listOf("--linux-package-name", "hellofx", "--linux-shortcut"))
        } else if (currentOs.isMacOsX) {
            installerOptions.addAll(listOf("--mac-package-name", "hellofx"))
        }
    }
}
