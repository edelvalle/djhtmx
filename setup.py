from pathlib import Path

from setuptools import setup, find_packages


HERE = Path(__file__).parent
README = HERE.joinpath('README.md').open(encoding='utf-8').read()


setup(
    name="djhtmx",
    version="0.0.0",
    url='https://github.com/edelvalle/djhtmx',
    author='Eddy Ernesto del Valle Pino',
    author_email='eddy@edelvalle.me',
    long_description=README,
    long_description_content_type='text/markdown',
    description="Brings LiveView from Phoenix framework into Django",
    license='MIT',
    packages=find_packages(exclude=['tests']),
    include_package_data=True,
    zip_safe=False,
    python_requires='>=3.6',
    install_requires=[
        'pydantic>=1,<2',
    ],
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Web Environment',
        'Framework :: Django',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Topic :: Internet :: WWW/HTTP',
    ],
)
