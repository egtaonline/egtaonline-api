import setuptools


setuptools.setup(
    name='egtaonlineapi',
    version='0.2.2',
    description='Various APIs for egtaonline',
    url='https://github.com/egtaonline/egtaonline-api.git',
    author='Strategic Reasoning Group',
    author_email='strategic.reasoning.group@umich.edu',
    license='Apache 2.0',
    entry_points=dict(console_scripts=['eo=egtaonline.__main__:main']),
    install_requires=[
        'lxml~=3.8',
        'requests~=2.18',
        'tabulate~=0.8',
        'inflection~=0.3',
    ],
    packages=[
        'egtaonline',
    ]
)
