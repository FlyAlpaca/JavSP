import os

from cx_Freeze import Executable, setup

# https://github.com/marcelotduarte/cx_Freeze/issues/1288
base = None

proj_root = os.path.abspath(os.path.dirname(__file__))


include_files: list[tuple[str, str]] = [
    (f"{proj_root}/config_default.yml", "config_default.yml"),
    (f"{proj_root}/data", "data"),
    (f"{proj_root}/image", "image"),
]

# 如果有 VERSION 文件（CI 构建时写入），将其打入产物以支持版本识别
version_file_src = f"{proj_root}/javsp/VERSION"
version_file_dst = "javsp/VERSION"
if os.path.exists(version_file_src):
    include_files.append((version_file_src, version_file_dst))

includes = []

for file in os.listdir("javsp/web"):
    name, ext = os.path.splitext(file)
    if ext == ".py":
        includes.append("javsp.web." + name)

packages = [
    "pendulum",  # pydantic_extra_types depends on pendulum
]

build_exe = {
    "include_files": include_files,
    "includes": includes,
    "excludes": ["unittest"],
    "packages": packages,
    "silent": True,
}

javsp = Executable(
    "./javsp/__main__.py",
    target_name="JavSP-bin",
    base=base,
    icon="./image/JavSP.ico",
)

setup(name="JavSP", options={"build_exe": build_exe}, executables=[javsp])
