#!/usr/bin/env bash
# MaixCDK 编译容器便捷脚本
#   用法: ./scripts/maixcdk-docker.sh          # 进入容器交互 shell
#         ./scripts/maixcdk-docker.sh stop     # 停止容器
#         ./scripts/maixcdk-docker.sh rm       # 删除容器
#         ./scripts/maixcdk-docker.sh rebuild  # 重新拉镜像
set -euo pipefail

WORKSPACE="/media/felix/892G/ai/maxicam"
IMAGE="sipeed/maixcdk-builder"
CONTAINER="maixcdk-env"
USER_NAME="$(id -un)"
UID_NUM="$(id -u)"
GID_NUM="$(id -g)"

case "${1:-shell}" in
  shell)
    if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
      # 容器已存在：启动并 attach
      docker start "$CONTAINER" >/dev/null
      exec docker attach "$CONTAINER"
    fi
    echo ">> 首次创建容器（映射工作区，用户 ${USER_NAME} ${UID_NUM}:${GID_NUM}）"
    exec docker run -it --network=host \
      --hostname maixcdk-env --name "$CONTAINER" \
      --env USER="$USER_NAME" --env UID="$UID_NUM" --env GID="$GID_NUM" \
      --env MAIXCDK_PATH="/home/${USER_NAME}/MaixCDK" \
      -v "${WORKSPACE}/MaixCDK:/home/${USER_NAME}/MaixCDK" \
      -v "${WORKSPACE}/projects:/home/${USER_NAME}/projects" \
      "$IMAGE"
    ;;
  stop)    docker stop "$CONTAINER" ;;
  rm)      docker stop "$CONTAINER" 2>/dev/null || true; docker rm "$CONTAINER" ;;
  rebuild) docker pull "$IMAGE" ;;
  *)       echo "用法: $0 [shell|stop|rm|rebuild]"; exit 1 ;;
esac
