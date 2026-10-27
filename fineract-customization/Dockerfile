# syntax=docker/dockerfile:1.7
# Builds Apache Fineract from the pinned upstream tag with patches/erp-gl-sync.patch applied,
# then bakes the jar into a slim JRE image. Java tests run outside Docker (see README).
ARG JDK_IMAGE=eclipse-temurin:21.0.11_10-jdk-noble
ARG JRE_IMAGE=eclipse-temurin:21.0.11_10-jre-noble

FROM ${JDK_IMAGE} AS build
ARG FINERACT_TAG=1.15.0
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch "${FINERACT_TAG}" https://github.com/apache/fineract.git .
COPY patches/ /patches/
RUN if [ -s /patches/erp-gl-sync.patch ]; then git apply --verbose /patches/erp-gl-sync.patch; \
    else echo "No patch: building unmodified ${FINERACT_TAG}"; fi
RUN --mount=type=cache,target=/root/.gradle \
    ./gradlew --no-daemon :fineract-provider:bootJar \
      -x test -x rat -x spotlessCheck -x checkstyleMain -x spotbugsMain \
 && cp fineract-provider/build/libs/fineract-provider-*.jar /fineract.jar

FROM ${JRE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 fineract
COPY --from=build /fineract.jar /app/fineract.jar
USER fineract
EXPOSE 8443
ENTRYPOINT ["java", "-jar", "/app/fineract.jar"]
