Copyright (c) Blaine Rister 2018

This package can quickly warp images using C or python. The build system requires CMake, as well as a C++ compiler with CUDA. To install on Ubuntu 16.04, do

        mkdir build && cd build
        cmake ..
        make

To install the python package, do 
       
        pip install -e .

from the build directory. For usage examples, see tester.c and test.py in the 'test' subdirectory.
