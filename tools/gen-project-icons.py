"""生成「新建项目」弹窗可选的项目图标子集（plan-20261009-ui U1/U3）。

素材：Phosphor Icons（MIT License, Copyright (c) 2023 Phosphor Icons）duotone 字重，
     经 npm 包 @phosphor-icons/core 的 assets/duotone/*.svg 发布（viewBox 0 0 256 256）。

duotone 图标由两层组成：opacity=0.2 的「底色层」+ 实线轮廓层。生成时把底色层标为 class="ph-duo"，
由 styles/icons.css 控制其透明度（闲置浅、悬停/选中加深）——这正是项目图标的「状态变更」。

用法：
    npm pack @phosphor-icons/core
    tar -xzf phosphor-icons-core-*.tgz -C <dir>
    python tools/gen-project-icons.py <dir>/package
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "client", "src", "components", "project", "projectIconData.ts")

# (落库 key, Phosphor 名称, 中文名)。key 一经发布不可改名（会落库），只能新增。
ICONS = [
    # 工作类
    ("folder", "folder-simple", "文件夹"),
    ("code", "code", "代码"),
    ("terminal", "terminal-window", "终端"),
    ("cube", "cube", "模块"),
    ("database", "database", "数据库"),
    ("cpu", "cpu", "芯片"),
    ("bug", "bug", "调试"),
    ("flask", "flask", "实验"),
    ("rocket", "rocket-launch", "发布"),
    ("lightning", "lightning", "闪电"),
    ("globe", "globe-hemisphere-west", "网站"),
    ("robot", "robot", "机器人"),
    ("brain", "brain", "智能"),
    ("puzzle", "puzzle-piece", "插件"),
    ("chart", "chart-line-up", "增长"),
    # 趣味类
    ("coffee", "coffee", "咖啡"),
    ("martini", "martini", "鸡尾酒"),
    ("cake", "cake", "蛋糕"),
    ("game", "game-controller", "游戏"),
    ("ghost", "ghost", "幽灵"),
    ("alien", "alien", "外星人"),
    ("cat", "cat", "猫咪"),
    ("butterfly", "butterfly", "蝴蝶"),
    ("cactus", "cactus", "仙人掌"),
    ("planet", "planet", "星球"),
    ("fire", "fire", "火焰"),
    ("sparkle", "sparkle", "灵感"),
    ("crown", "crown", "皇冠"),
    ("palette", "palette", "调色板"),
    ("lego", "lego-smiley", "积木"),
]


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    base = os.path.join(sys.argv[1], "assets", "duotone")
    rows = []
    for key, ph, label in ICONS:
        svg = open(os.path.join(base, f"{ph}-duotone.svg"), encoding="utf-8").read()
        inner = re.sub(r"^.*?<svg[^>]*>|</svg>\s*$", "", svg, flags=re.S)
        paths = re.findall(r"<path[^>]*/>", inner)
        duo = "".join(p.replace(' opacity="0.2"', "") for p in paths if 'opacity="0.2"' in p)
        line = "".join(p for p in paths if 'opacity="0.2"' not in p)
        if not duo or not line:
            raise SystemExit(f"{ph} 不是标准 duotone 结构")
        rows.append((key, label, duo, line))

    out = [
        "/* eslint-disable */",
        "/** 自动生成（tools/gen-project-icons.py），请勿手改。",
        " *  项目图标子集 —— Phosphor Icons duotone（MIT License, Copyright (c) 2023 Phosphor Icons），",
        " *  viewBox 0 0 256 256；duo = 底色层（透明度由 CSS 控制），line = 轮廓层。 */",
        "",
        "export interface ProjectIconDef { key: string; label: string; duo: string; line: string }",
        "",
        "export const PROJECT_ICON_DEFS: ProjectIconDef[] = [",
    ]
    for key, label, duo, line in rows:
        out.append(f"  {{ key: {json.dumps(key)}, label: {json.dumps(label, ensure_ascii=False)}, duo: {json.dumps(duo)}, line: {json.dumps(line)} }},")
    out.append("];")
    out.append("")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out))
    print(f"写入 {OUT}：{len(rows)} 个图标")


if __name__ == "__main__":
    main()
