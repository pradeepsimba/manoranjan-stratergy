plugins {
    java
    id("org.springframework.boot") version "3.4.5"
    id("io.spring.dependency-management") version "1.1.7"
}

group = "com.trading"
version = "1.0.0"

java {
    sourceCompatibility = JavaVersion.VERSION_21
    targetCompatibility = JavaVersion.VERSION_21
}

repositories {
    mavenCentral()
}

dependencies {
    // Web (REST + embedded Tomcat)
    implementation("org.springframework.boot:spring-boot-starter-web")

    // WebSocket — push live updates to browser
    implementation("org.springframework.boot:spring-boot-starter-websocket")

    // JDBC template for SQLite
    implementation("org.springframework.boot:spring-boot-starter-jdbc")

    // SQLite driver
    implementation("org.xerial:sqlite-jdbc:3.44.1.0")

    // JSON
    implementation("com.fasterxml.jackson.core:jackson-databind")
}

tasks.withType<JavaCompile> {
    options.encoding = "UTF-8"
}
