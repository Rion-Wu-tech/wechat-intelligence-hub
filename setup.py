"""Setup configuration for WeChat Slim."""
from setuptools import setup, find_packages

with open("README.md", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="wechat-slim",
    version="1.0.0",
    author="LuckTerence",
    author_email="xihuan1127@gmail.com",
    description="WeChat Slim - 微信智能无损瘦身工具 (Mac 版)",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/LuckTerence/wechat-intelligence-hub",
    project_urls={
        "Bug Tracker": "https://github.com/LuckTerence/wechat-intelligence-hub/issues",
        "Documentation": "https://github.com/LuckTerence/wechat-intelligence-hub/blob/main/docs/USAGE.md",
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Intended Audience :: End Users/Desktop",
        "License :: OSI Approved :: MIT License",
        "Operating System :: MacOS :: MacOS X",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Utilities",
        "Topic :: System :: Archiving :: Cleanup",
    ],
    keywords="wechat wechat slim cleanup storage mac optimization",
    packages=find_packages(where="projects"),
    py_modules=["wechat_slim"],
    package_dir={"": "projects"},
    package_data={
        "wechat_intelligence_hub": ["config/*.yaml", "locale/*.json"],
    },
    include_package_data=True,
    python_requires=">=3.8,<4.0",
    entry_points={
        "console_scripts": [
            "wechat-slim=wechat_slim:main",
        ],
    },
    install_requires=[],  # 零依赖！全部使用标准库
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "black>=23.1.0",
            "mypy>=1.0.0",
        ],
        "webui": [
            "streamlit>=1.25.0",
        ],
    },
)
