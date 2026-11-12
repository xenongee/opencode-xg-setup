from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).parent
DATA_ROOTS = (Path("setup/instructions"), Path("setup/mcp"), Path("setup/skills"), Path("setup/plugins"))
DATA_SUFFIXES = {".json", ".toml", ".lock", ".md", ".py", ".ts"}
DATA_NAMES = {"LICENSE"}
ROOT_DATA = (Path("packages.json"),)


def runtime_data_files():
    files = list(ROOT_DATA)
    for directory in DATA_ROOTS:
        files.extend(
            path.relative_to(ROOT)
            for path in (ROOT / directory).rglob("*")
            if path.is_file() and "node_modules" not in path.parts
            and (path.suffix in DATA_SUFFIXES or path.name in DATA_NAMES)
        )
    return sorted(files)


class build_py_with_runtime_data(build_py):
    def run(self):
        super().run()
        for relative in runtime_data_files():
            source = ROOT / relative
            target = Path(self.build_lib) / "data" / relative
            self.mkpath(str(target.parent))
            self.copy_file(str(source), str(target))

    def get_outputs(self, include_bytecode=1):
        outputs = super().get_outputs(include_bytecode)
        outputs.extend(
            str(Path(self.build_lib) / "data" / relative)
            for relative in runtime_data_files()
        )
        return outputs

    def get_source_files(self):
        return super().get_source_files() + [str(path) for path in runtime_data_files()]


setup(cmdclass={"build_py": build_py_with_runtime_data})
