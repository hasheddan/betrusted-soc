#!/bin/sh

if [ ! -e betrusted-pac ]
then
   mkdir betrusted-pac
fi
cd betrusted-pac
svd2rust --target riscv -i ../../build/software/soc.svd
rm -rf src; form -i lib.rs -o src/; rm lib.rs
cargo doc
