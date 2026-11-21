# Motor oficial da Calculadora de Tributos (Receita Federal / Serpro).
# Uma única imagem contém os dois motores; a variável ENGINE escolhe qual sobe:
#   ENGINE=regular  -> calculadora-rtc-regime-regular  (porta interna 8080)
#   ENGINE=simples  -> calculadora-rtc-simples-nacional (porta interna 8082)
# Em hospedagens que definem PORT (Render, Cloud Run), o motor escuta nela.

# ---------- build ----------
# Imagem com Maven + JDK 25 (o mvnw dos repositórios não traz o maven-wrapper.jar, que está no .gitignore).
FROM maven:3.9-eclipse-temurin-25 AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Fixe tags/commits aqui para builds reproduzíveis (padrão: main).
ARG REGULAR_REF=main
ARG SIMPLES_REF=main

WORKDIR /src
RUN git clone --depth 1 --branch "${REGULAR_REF}" \
        https://github.com/ReceitaFederal/calculadora-rtc-regime-regular.git regular \
    && git clone --depth 1 --branch "${SIMPLES_REF}" \
        https://github.com/ReceitaFederal/calculadora-rtc-simples-nacional.git simples

# 1) Gera o banco SQLite com o Flyway do repositório do regime regular.
WORKDIR /src/regular
RUN mkdir -p calculadora/db \
    && mvn -B flyway:migrate -Dflyway.configFiles=./flyway/flyway-pro.conf \
    && test -s calculadora/db/calculadora-pro.db

# 2) Compila os dois motores.
RUN mvn -B -DskipTests clean package
WORKDIR /src/simples
RUN mvn -B -DskipTests clean package

# 3) Monta a estrutura final (cada motor lê ./calculadora/db a partir do seu diretório).
RUN mkdir -p /out/regular/calculadora/db /out/simples/calculadora/db \
    && cp "$(ls /src/regular/target/*.jar | grep -v original | head -n1)" /out/regular/app.jar \
    && cp "$(ls /src/simples/target/*.jar | grep -v original | head -n1)" /out/simples/app.jar \
    && cp /src/regular/calculadora/db/calculadora-pro.db /out/regular/calculadora/db/ \
    && cp /src/regular/calculadora/db/calculadora-pro.db /out/simples/calculadora/db/

# ---------- runtime ----------
FROM eclipse-temurin:25-jre

RUN useradd --system --create-home app
WORKDIR /app
COPY --from=build --chown=app:app /out /app
COPY --chown=app:app entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER app
ENV ENGINE=regular \
    JAVA_OPTS="-Xmx384m -XX:+UseSerialGC"
EXPOSE 8080 8082
ENTRYPOINT ["/app/entrypoint.sh"]
