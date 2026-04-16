#!/bin/bash

# 数据备份和恢复脚本

DATA_DIR="./data"
BACKUP_DIR="./data_backup"
VOLUME_NAME="person_fin_person_fin_data"

# 显示帮助
show_help() {
    echo "用法: $0 [backup|restore|migrate]"
    echo ""
    echo "命令:"
    echo "  backup  - 备份当前数据到 data_backup 目录"
    echo "  restore - 从 data_backup 目录恢复数据"
    echo "  migrate - 将本地数据迁移到 Docker 卷"
    echo ""
}

# 备份数据
backup_data() {
    echo "正在备份数据..."
    if [ -f "$DATA_DIR/person_fin.db" ]; then
        mkdir -p "$BACKUP_DIR"
        cp "$DATA_DIR/person_fin.db" "$BACKUP_DIR/"
        echo "✓ 数据已备份到 $BACKUP_DIR/person_fin.db"
    else
        echo "✗ 未找到数据库文件: $DATA_DIR/person_fin.db"
        exit 1
    fi
}

# 恢复数据
restore_data() {
    echo "正在恢复数据..."
    if [ -f "$BACKUP_DIR/person_fin.db" ]; then
        mkdir -p "$DATA_DIR"
        cp "$BACKUP_DIR/person_fin.db" "$DATA_DIR/"
        echo "✓ 数据已从 $BACKUP_DIR 恢复到 $DATA_DIR"
    else
        echo "✗ 未找到备份文件: $BACKUP_DIR/person_fin.db"
        exit 1
    fi
}

# 迁移数据到 Docker 卷
migrate_to_volume() {
    echo "正在将数据迁移到 Docker 卷..."

    # 确保容器正在运行
    if ! docker-compose ps | grep -q "backend"; then
        echo "✗ 后端容器未运行，请先启动容器"
        exit 1
    fi

    # 备份当前数据
    if [ -f "$DATA_DIR/person_fin.db" ]; then
        backup_data
    fi

    # 复制数据到容器
    echo "正在复制数据到 Docker 容器..."
    docker cp "$BACKUP_DIR/person_fin.db" "person_fin-backend-1:/app/data/person_fin.db"

    # 重启后端服务
    echo "正在重启后端服务..."
    docker-compose restart backend

    echo "✓ 数据迁移完成"
}

# 主逻辑
case "$1" in
    backup)
        backup_data
        ;;
    restore)
        restore_data
        ;;
    migrate)
        migrate_to_volume
        ;;
    *)
        show_help
        exit 1
        ;;
esac
