import setuptools


setuptools.setup(
  name="egtaonlineapi",
  version="0.2",
  description="Various APIs for egtaonline",
  url="https://github.com/egtaonline/egtaonline-api.git",
  author="Strategic Reasoning Group",
  license="MIT",
  entry_points=dict(console_scripts=["eo=egtaonline.eo:main"]),
  install_requires=[
    "lxml~=3.6",
    "requests~=2.11",
    "tabulate~=0.7",
    "inflection~=0.3"
  ],
  packages=[
    "egtaonline"
  ]
)
