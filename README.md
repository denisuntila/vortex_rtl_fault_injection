# Vortex RTL Fault Injection

This repository contains research and implementation for fault injection experiments conducted on the **Vortex GPGPU** architecture, developed as part of thesis work.

---

## Getting Started

### Prerequisites
Ensure your environment is set up correctly. This project is tested and verified using an **Ubuntu 22.04** container environment.

### Installation
Clone the repository and initialize the necessary submodules:

```bash
# Clone the repository
git clone https://github.com/denisuntila/vortex_rtl_fault_injection.git
cd vortex_rtl_fault_injection

# Initialize and update submodules
git submodule update --init --recursive
```

### Building the Project
Run the provided build script to compile the necessary components:


```bash
./run_build.sh
```

Usage
To execute the fault injection tests, navigate to the specific test directory within the build/tests folder.

```bash
# Navigate to the test directory
cd build/tests/regression/vecadd

# Run the test
LD_LIBRARY_PATH=../../../runtime VORTEX_DRIVER=rtlsim ./vecadd -n64

```

