"""生成右侧文件树 / 产物列表使用的文件类型图标子集（plan-20261009-ui U3/U8）。

素材：Material Icon Theme（MIT License, Copyright (c) 2025 Material Extensions）
     经 @iconify-json/material-icon-theme 发布的 icons.json（16×16 viewBox）。

为什么「生成子集」而不是引入依赖：完整图标集 ~900KB，应用只需要约 80 个常用类型；
这里只把用到的路径数据写入 client/src/components/iconkit/fileIconData.ts，零运行时依赖。

用法（在任意目录执行，参数为解包后的 npm 包目录）：
    npm pack @iconify-json/material-icon-theme
    tar -xzf iconify-json-material-icon-theme-*.tgz -C <dir>
    python tools/gen-file-icons.py <dir>/package
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "client", "src", "components", "iconkit", "fileIconData.ts")

# 扩展名 → 图标名（按优先级给出候选，第一个存在的生效）
EXT = {
    "ts": ["typescript"], "mts": ["typescript"], "cts": ["typescript"],
    "tsx": ["react-ts"], "jsx": ["react"],
    "js": ["javascript"], "mjs": ["javascript"], "cjs": ["javascript"],
    "json": ["json"], "jsonc": ["json"], "json5": ["json"],
    "md": ["markdown"], "markdown": ["markdown"], "mdx": ["mdx"],
    "txt": ["document"], "log": ["log"],
    "css": ["css"], "scss": ["sass"], "sass": ["sass"], "less": ["less"],
    "html": ["html"], "htm": ["html"], "vue": ["vue"], "svelte": ["svelte"], "astro": ["astro"],
    "py": ["python"], "pyw": ["python"], "pyi": ["python"], "ipynb": ["jupyter", "python"],
    "java": ["java"], "kt": ["kotlin"], "kts": ["kotlin"], "go": ["go"], "rs": ["rust"],
    "c": ["c"], "h": ["h", "c"], "cpp": ["cpp"], "cc": ["cpp"], "cxx": ["cpp"], "hpp": ["hpp", "cpp"],
    "cs": ["csharp"], "php": ["php"], "rb": ["ruby"], "swift": ["swift"], "dart": ["dart"], "lua": ["lua"],
    "yml": ["yaml"], "yaml": ["yaml"], "toml": ["toml"], "xml": ["xml"],
    "ini": ["settings"], "cfg": ["settings"], "conf": ["settings"], "properties": ["settings"],
    "env": ["tune", "settings"],
    "sh": ["console"], "bash": ["console"], "zsh": ["console"], "bat": ["console"], "cmd": ["console"],
    "ps1": ["powershell"], "psm1": ["powershell"],
    "sql": ["database"], "sqlite": ["database"], "db": ["database"],
    "png": ["image"], "jpg": ["image"], "jpeg": ["image"], "gif": ["image"], "webp": ["image"],
    "bmp": ["image"], "ico": ["image"], "avif": ["image"], "svg": ["svg"],
    "mp3": ["audio"], "wav": ["audio"], "flac": ["audio"], "ogg": ["audio"],
    "mp4": ["video"], "mov": ["video"], "webm": ["video"], "mkv": ["video"],
    "zip": ["zip"], "rar": ["zip"], "7z": ["zip"], "tar": ["zip"], "gz": ["zip"], "tgz": ["zip"],
    "pdf": ["pdf"], "doc": ["word"], "docx": ["word"], "ppt": ["powerpoint"], "pptx": ["powerpoint"],
    "xls": ["table"], "xlsx": ["table"], "csv": ["table"], "tsv": ["table"],
    "ttf": ["font"], "otf": ["font"], "woff": ["font"], "woff2": ["font"],
    "lock": ["lock"], "exe": ["exe"], "dll": ["dll"], "msi": ["exe"],
    "proto": ["proto"], "graphql": ["graphql"], "gql": ["graphql"], "prisma": ["prisma"],
    "crt": ["certificate"], "pem": ["certificate"], "cer": ["certificate"], "key": ["key"],
    "http": ["http"], "rest": ["http"], "tf": ["terraform"], "gradle": ["gradle"],
    "diff": ["diff"], "patch": ["diff"], "map": ["javascript-map"],
}

# 完整文件名（小写）→ 图标名
NAME = {
    "package.json": ["nodejs"], "package-lock.json": ["npm", "nodejs"], ".npmrc": ["npm"],
    "yarn.lock": ["yarn"], "pnpm-lock.yaml": ["pnpm"], "pnpm-workspace.yaml": ["pnpm"],
    "jsconfig.json": ["jsconfig"], ".babelrc": ["babel"], ".editorconfig": ["editorconfig"],
    "dockerfile": ["docker"], ".dockerignore": ["docker"],
    ".gitignore": ["git"], ".gitattributes": ["git"], ".gitmodules": ["git"],
    "makefile": ["makefile"], "cmakelists.txt": ["cmake"],
    "requirements.txt": ["python-misc", "python"], "pyproject.toml": ["python-misc", "python"],
    "favicon.ico": ["favicon"], "robots.txt": ["robots"],
    "claude.md": ["claude", "markdown"], "agents.md": ["markdown"],
    ".env": ["tune", "settings"],
}

# 文件名前缀（小写）→ 图标名（按顺序匹配，用于 tsconfig.*.json / vite.config.* 等）
PREFIX = [
    ("tsconfig", ["tsconfig"]), ("vite.config", ["vite"]), ("vitest.config", ["vitest"]),
    ("webpack.config", ["webpack"]), ("rollup.config", ["rollup"]), ("babel.config", ["babel"]),
    ("jest.config", ["jest"]), (".eslintrc", ["eslint"]), ("eslint.config", ["eslint"]),
    (".prettierrc", ["prettier"]), ("prettier.config", ["prettier"]),
    ("docker-compose", ["docker"]), ("compose.y", ["docker"]),
    ("readme", ["readme"]), ("license", ["license"]), ("licence", ["license"]), ("changelog", ["changelog"]),
    ("tailwind.config", ["tailwindcss"]), ("next.config", ["next"]), ("nuxt.config", ["nuxt"]),
    ("svelte.config", ["svelte"]), ("astro.config", ["astro-config"]), (".env.", ["tune", "settings"]),
]

# 文件夹名（小写）→ 文件夹图标基名（自动配 -open 版本）
FOLDER = {
    "src": "folder-src", "source": "folder-src",
    "components": "folder-components", "hooks": "folder-hook", "hook": "folder-hook",
    "utils": "folder-utils", "util": "folder-utils", "helpers": "folder-utils",
    "lib": "folder-lib", "libs": "folder-lib", "public": "folder-public", "static": "folder-public",
    "assets": "folder-resource", "resources": "folder-resource", "res": "folder-resource",
    "images": "folder-images", "img": "folder-images", "icons": "folder-images",
    "styles": "folder-css", "css": "folder-css", "scss": "folder-sass", "sass": "folder-sass",
    "api": "folder-api", "server": "folder-server", "backend": "folder-server",
    "client": "folder-client", "frontend": "folder-client", "web": "folder-client",
    "db": "folder-database", "database": "folder-database", "persistence": "folder-database",
    "routes": "folder-routes", "router": "folder-routes", "routers": "folder-routes",
    "layouts": "folder-layout", "layout": "folder-layout",
    "views": "folder-views", "pages": "folder-views", "screens": "folder-views",
    "store": "folder-store", "stores": "folder-store", "state": "folder-store",
    "i18n": "folder-i18n", "locales": "folder-i18n", "lang": "folder-i18n",
    "plugins": "folder-plugin", "plugin": "folder-plugin", "extensions": "folder-plugin",
    "packages": "folder-packages", "tools": "folder-tools", "scripts": "folder-scripts", "bin": "folder-scripts",
    "config": "folder-config", "configs": "folder-config", "settings": "folder-config",
    "docs": "folder-docs", "doc": "folder-docs",
    "test": "folder-test", "tests": "folder-test", "__tests__": "folder-test", "spec": "folder-test", "e2e": "folder-test",
    "dist": "folder-dist", "build": "folder-dist", "out": "folder-dist", "release": "folder-dist",
    "target": "folder-target", "node_modules": "folder-node", ".git": "folder-git",
    ".github": "folder-github", ".vscode": "folder-vscode", "logs": "folder-log", "log": "folder-log",
    "temp": "folder-temp", "tmp": "folder-temp", "app": "folder-app", "mocks": "folder-mock",
    "__mocks__": "folder-mock", "shared": "folder-shared", "common": "folder-shared", "core": "folder-core",
    "content": "folder-content", "fonts": "folder-font", "audio": "folder-audio", "sounds": "folder-audio",
    "video": "folder-video", "videos": "folder-video", "coverage": "folder-coverage", "backup": "folder-backup",
    "desktop": "folder-desktop", "electron": "folder-desktop", "tasks": "folder-tasks",
    ".claude": "folder-claude", ".cursor": "folder-cursor", "prompts": "folder-prompts",
    "ci": "folder-ci", ".circleci": "folder-ci", ".venv": "folder-python", "venv": "folder-python",
    "types": "folder-typescript", "@types": "folder-typescript", "typings": "folder-typescript", "svg": "folder-svg",
    "ai": "folder-prompts", "secure": "folder-secure", "security": "folder-secure",
}

# 自绘兜底（Material 无通用文件图标）：灰蓝文件，配色与 Material 默认文件夹（#90a4ae）一致
DEFAULT_FILE = (
    '<path fill="#90a4ae" d="M4.5 1.5h4.79a1 1 0 0 1 .7.29l2.72 2.72a1 1 0 0 1 .29.7V13.5a1 1 0 0 1-1 1h-7.5a1 1 0 0 1-1-1v-11a1 1 0 0 1 1-1"/>'
    '<path fill="#cfd8dc" d="M9.5 1.75V4.5a.5.5 0 0 0 .5.5h2.75z"/>'
)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    data = json.load(open(os.path.join(sys.argv[1], "icons.json"), encoding="utf-8"))
    icons = data["icons"]
    aliases = data.get("aliases", {})

    default_w = int(data.get("width", 16))

    def body_of(name: str) -> list | None:
        """返回 [viewBox 边长, body]——部分图标为 24/32 网格，必须随图标保存。"""
        if name in icons:
            ic = icons[name]
            return [int(ic.get("width", default_w)), ic["body"]]
        if name in aliases:
            return body_of(aliases[name]["parent"])
        return None

    used: dict[str, list] = {}

    def pick(cands: list[str]) -> str | None:
        for c in cands:
            b = body_of(c)
            if b is not None:
                used[c] = b
                return c
        return None

    ext_map = {k: v for k, v in ((k, pick(c)) for k, c in EXT.items()) if v}
    name_map = {k: v for k, v in ((k, pick(c)) for k, c in NAME.items()) if v}
    prefix_map = [(k, v) for k, v in ((k, pick(c)) for k, c in PREFIX) if v]
    folder_map = {}
    for k, base in FOLDER.items():
        if pick([base]) and pick([base + "-open"]):
            folder_map[k] = base
    for base in ("folder-base", "folder-base-open", "typescript-def"):
        if not pick([base]):
            raise SystemExit(f"缺少必要图标：{base}")
    used["file"] = [16, DEFAULT_FILE]

    def ts_obj(d: dict) -> str:
        return "{\n" + "".join(f"  {json.dumps(k)}: {json.dumps(v)},\n" for k, v in sorted(d.items())) + "}"

    out = [
        "/* eslint-disable */",
        "/** 自动生成（tools/gen-file-icons.py），请勿手改。",
        " *  文件类型图标子集 —— Material Icon Theme（MIT License, Copyright (c) 2025 Material Extensions），",
        " *  路径数据来自 @iconify-json/material-icon-theme（viewBox 0 0 16 16）；`file` 为自绘兜底。",
        " *  plan-20261009-ui U3/U8：只内置用到的图标，零运行时依赖。 */",
        "",
        "/** 图标名 → [viewBox 边长, SVG 内容] */",
        f"export const FILE_ICON_BODIES: Record<string, [number, string]> = {ts_obj(used)};",
        "",
        f"export const FILE_EXT_MAP: Record<string, string> = {ts_obj(ext_map)};",
        "",
        f"export const FILE_NAME_MAP: Record<string, string> = {ts_obj(name_map)};",
        "",
        "export const FILE_PREFIX_MAP: Array<[string, string]> = [\n"
        + "".join(f"  [{json.dumps(k)}, {json.dumps(v)}],\n" for k, v in prefix_map) + "];",
        "",
        f"export const FOLDER_NAME_MAP: Record<string, string> = {ts_obj(folder_map)};",
        "",
    ]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out))
    print(f"写入 {OUT}：{len(used)} 个图标，扩展名 {len(ext_map)}，文件名 {len(name_map)}，"
          f"前缀 {len(prefix_map)}，文件夹 {len(folder_map)}")


if __name__ == "__main__":
    main()
