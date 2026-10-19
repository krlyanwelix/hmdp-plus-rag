# 主链路应用镜像：直接吃 mvn 打出来的 fat jar
#
#   mvn -o -q -DskipTests package
#   docker compose -f deploy/docker-compose.yml up -d --build
#
# 不在镜像里跑 Maven：构建慢、镜像大，而且 jar 已经在本地打好了。

FROM eclipse-temurin:8-jre

WORKDIR /app
COPY target/hmdp-plus-rag-0.0.1-SNAPSHOT.jar /app/app.jar

ENV TZ=Asia/Shanghai \
    JAVA_OPTS="-Xms256m -Xmx512m -Dfile.encoding=UTF-8"

EXPOSE 8081

# exec 让 java 成为 1 号进程，能收到 docker stop 的 SIGTERM 做优雅停机
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -jar /app/app.jar"]
