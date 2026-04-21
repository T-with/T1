#!/usr/bin/env python3
"""
install_phase2_3.py — 一键把 Phase 2 + Phase 3 的文件放到正确位置并应用

用法:
    把 phase2/ 和 phase3/ 两个目录放在项目根目录
    然后在项目根目录运行: python install_phase2_3.py

行为:
    1. 检查当前项目结构
    2. 复制模块文件到 engine/
    3. 复制测试到 tests/
    4. 运行 apply_phase2.py 和 apply_phase3.py
    5. 打印下一步指引
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()

# --- 预检查 ---
checks = [
    (ROOT / 'app.py', 'app.py'),
    (ROOT / 'engine' / 'core.py', 'engine/core.py'),
    (ROOT / 'tests', 'tests/'),
]
for p, name in checks:
    if not p.exists():
        print(f"❌ 不在项目根目录 — 找不到 {name}")
        sys.exit(1)

if not (ROOT / 'phase2').exists() or not (ROOT / 'phase3').exists():
    print("❌ 当前目录下没有 phase2/ 和 phase3/ 目录")
    print("   请先把 Claude 给的两个包解压/复制到这里")
    print(f"   当前目录: {ROOT}")
    sys.exit(1)

print(f"✅ 项目根目录: {ROOT}")

# --- 预备份旧 bug 文件,避免意外 ---
for fname in ('engine/core.py', 'engine/rl.py', 'app.py'):
    p = ROOT / fname
    if p.exists() and not p.with_suffix(p.suffix + '.pre-install.bak').exists():
        shutil.copy2(p, p.with_suffix(p.suffix + '.pre-install.bak'))
        print(f"  📦 预备份: {fname} → {fname}.pre-install.bak")


# --- 拷贝新模块 ---
print("\n[1/6] 拷贝 Phase 2 模块到 engine/")
for src_name in ('storage.py', 'reconciliation.py', 'kill_switch.py'):
    src = ROOT / 'phase2' / 'engine' / src_name
    dst = ROOT / 'engine' / src_name
    shutil.copy2(src, dst)
    print(f"  ✓ {src_name}")

print("\n[2/6] 拷贝 Phase 3 模块到 engine/")
for src_name in ('logging_setup.py', 'metrics.py', 'alerts.py'):
    src = ROOT / 'phase3' / 'engine' / src_name
    dst = ROOT / 'engine' / src_name
    shutil.copy2(src, dst)
    print(f"  ✓ {src_name}")

print("\n[3/6] 拷贝测试")
for src, dst in [
    (ROOT / 'phase2' / 'test_phase2.py', ROOT / 'tests' / 'test_phase2.py'),
    (ROOT / 'phase3' / 'test_phase3.py', ROOT / 'tests' / 'test_phase3.py'),
]:
    shutil.copy2(src, dst)
    print(f"  ✓ {dst.relative_to(ROOT)}")

print("\n[4/6] 拷贝 Phase 3 监控栈文件")
shutil.copy2(ROOT / 'phase3' / 'docker-compose.monitoring.yml',
             ROOT / 'docker-compose.monitoring.yml')
print("  ✓ docker-compose.monitoring.yml")

monitoring_dir = ROOT / 'monitoring'
if monitoring_dir.exists():
    print("  ⏭  monitoring/ 已存在, 跳过")
else:
    shutil.copytree(ROOT / 'phase3' / 'monitoring', monitoring_dir)
    print("  ✓ monitoring/ (prometheus + grafana 配置)")

print("\n[5/6] 应用 Phase 2 到 app.py")
r = subprocess.run([sys.executable, str(ROOT / 'phase2' / 'apply_phase2.py')],
                   cwd=ROOT, capture_output=True, text=True)
print(r.stdout)
if r.returncode != 0:
    print(r.stderr)
    print("⚠️  Phase 2 应用失败 — 请手动检查")

print("\n[6/6] 应用 Phase 3 到 app.py")
r = subprocess.run([sys.executable, str(ROOT / 'phase3' / 'apply_phase3.py')],
                   cwd=ROOT, capture_output=True, text=True)
print(r.stdout)
if r.returncode != 0:
    print(r.stderr)
    print("⚠️  Phase 3 应用失败")
else:
    # 只有 phase3 应用成功才跑 instrumentation (它改 engine/core.py)
    r2 = subprocess.run([sys.executable, str(ROOT / 'phase3' / 'apply_phase3_instrumentation.py')],
                        cwd=ROOT, capture_output=True, text=True)
    print(r2.stdout)

print("\n" + "=" * 60)
print("✅ 安装脚本执行完成")
print()
print("下一步:")
print("  1. 跑测试: python -m pytest tests/ -v")
print("     期望: 48 (原) + Phase 2 + Phase 3 = 70+ 全过")
print()
print("  2. (可选) 配告警渠道,编辑 .env 追加:")
print("     参考 phase3/alerts.env.example")
print()
print("  3. 启动带监控栈:")
print("     docker compose down")
print("     docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d --build")
print()
print("  4. 验证:")
print("     curl http://localhost:8080/metrics")
print("     浏览器打开 http://localhost:3000 (admin/admin)")
print("=" * 60)
