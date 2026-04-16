#!/usr/bin/env python3
"""
数据迁移脚本：将 liquidity_rating 字符串字段迁移到独立表

迁移步骤：
1. 创建 LiquidityRating 表
2. 从现有数据中提取唯一的 liquidity_rating 值
3. 插入到 LiquidityRating 表
4. 更新 AssetRecord 表，设置 liquidity_rating_id 外键
5. 删除旧的 liquidity_rating 列（可选，建议保留作为备份）

使用方法：
    cd backend
    python migrate_liquidity_rating.py
"""

import os
import sys
from datetime import datetime

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker
from database import engine, Base
from models import LiquidityRating, AssetRecord


def migrate_liquidity_rating():
    """执行数据迁移"""
    print("=" * 60)
    print("开始数据迁移：liquidity_rating 字段迁移到独立表")
    print("=" * 60)
    
    # 创建 session
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    
    try:
        # 步骤 1: 检查是否已经迁移过
        print("\n[步骤 1] 检查迁移状态...")
        inspector = inspect(engine)
        columns = inspector.get_columns('asset_records')
        column_names = [c['name'] for c in columns]
        
        if 'liquidity_rating_id' in column_names:
            print("✓ 检测到 liquidity_rating_id 列已存在，检查是否需要数据迁移...")
            
            # 检查是否还有数据使用旧的字符串字段
            old_records = db.execute(
                text("SELECT COUNT(*) FROM asset_records WHERE liquidity_rating IS NOT NULL")
            ).scalar()
            
            if old_records == 0:
                print("✓ 数据已迁移完成，无需重复操作")
                return
            else:
                print(f"! 发现 {old_records} 条记录需要迁移")
        else:
            print("✗ 请先更新数据库模型并创建新表")
            print("  运行: 删除旧数据库或执行 Base.metadata.create_all()")
            return
        
        # 步骤 2: 创建 LiquidityRating 表（如果不存在）
        print("\n[步骤 2] 创建 LiquidityRating 表...")
        Base.metadata.create_all(bind=engine, tables=[LiquidityRating.__table__])
        print("✓ LiquidityRating 表已创建")
        
        # 步骤 3: 从现有数据提取唯一的 liquidity_rating 值
        print("\n[步骤 3] 提取唯一的流动性评级值...")
        unique_ratings = db.execute(
            text("SELECT DISTINCT liquidity_rating FROM asset_records WHERE liquidity_rating IS NOT NULL")
        ).scalars().all()
        
        print(f"  发现 {len(unique_ratings)} 个唯一的流动性评级:")
        for rating in unique_ratings:
            print(f"    - {rating}")
        
        # 步骤 4: 插入到 LiquidityRating 表
        print("\n[步骤 4] 创建 LiquidityRating 记录...")
        rating_id_map = {}  # 用于存储 name -> id 的映射
        
        for idx, rating_name in enumerate(unique_ratings, 1):
            # 检查是否已存在
            existing = db.query(LiquidityRating).filter(
                LiquidityRating.name == rating_name
            ).first()
            
            if existing:
                rating_id_map[rating_name] = existing.id
                print(f"  已存在: {rating_name} (ID: {existing.id})")
            else:
                new_rating = LiquidityRating(
                    name=rating_name,
                    sort_order=idx,
                    created_at=datetime.now()
                )
                db.add(new_rating)
                db.flush()  # 获取 ID
                rating_id_map[rating_name] = new_rating.id
                print(f"  创建: {rating_name} (ID: {new_rating.id})")
        
        db.commit()
        print(f"✓ 共创建/更新 {len(rating_id_map)} 个流动性评级")
        
        # 步骤 5: 更新 AssetRecord 表
        print("\n[步骤 5] 更新 AssetRecord 表的 liquidity_rating_id...")
        
        for rating_name, rating_id in rating_id_map.items():
            result = db.execute(
                text("""
                    UPDATE asset_records 
                    SET liquidity_rating_id = :rating_id 
                    WHERE liquidity_rating = :rating_name
                """),
                {"rating_id": rating_id, "rating_name": rating_name}
            )
            print(f"  更新 '{rating_name}': {result.rowcount} 条记录")
        
        db.commit()
        print("✓ AssetRecord 表更新完成")
        
        # 步骤 6: 验证迁移结果
        print("\n[步骤 6] 验证迁移结果...")
        total_records = db.execute(text("SELECT COUNT(*) FROM asset_records")).scalar()
        migrated_records = db.execute(
            text("SELECT COUNT(*) FROM asset_records WHERE liquidity_rating_id IS NOT NULL")
        ).scalar()
        
        print(f"  总记录数: {total_records}")
        print(f"  已迁移记录数: {migrated_records}")
        
        if total_records == migrated_records:
            print("✓ 所有记录迁移成功！")
        else:
            print(f"! 警告: 有 {total_records - migrated_records} 条记录未迁移")
        
        # 显示统计信息
        print("\n[统计信息]")
        stats = db.execute(
            text("""
                SELECT lr.name, COUNT(ar.id) as count
                FROM liquidity_ratings lr
                LEFT JOIN asset_records ar ON lr.id = ar.liquidity_rating_id
                GROUP BY lr.id, lr.name
                ORDER BY lr.sort_order
            """)
        ).fetchall()
        
        for name, count in stats:
            print(f"  {name}: {count} 条记录")
        
        print("\n" + "=" * 60)
        print("数据迁移完成！")
        print("=" * 60)
        
    except Exception as e:
        db.rollback()
        print(f"\n✗ 迁移失败: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        db.close()


def rollback_migration():
    """回滚迁移（谨慎使用）"""
    print("=" * 60)
    print("回滚数据迁移")
    print("=" * 60)
    
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    
    try:
        # 这里可以实现回滚逻辑
        print("回滚功能未实现，请手动处理")
        
    except Exception as e:
        print(f"回滚失败: {str(e)}")
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="流动性评级数据迁移脚本")
    parser.add_argument(
        "--rollback", 
        action="store_true", 
        help="回滚迁移（谨慎使用）"
    )
    
    args = parser.parse_args()
    
    if args.rollback:
        rollback_migration()
    else:
        migrate_liquidity_rating()
