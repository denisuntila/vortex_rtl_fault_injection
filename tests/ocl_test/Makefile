ROOT_DIR := $(realpath ../../..)
include $(ROOT_DIR)/config.mk

PROJECT := ocl_test

SRC_DIR := $(ROOT_DIR)/../tests/$(PROJECT)

SRCS := $(SRC_DIR)/main.cc

kernel.cl: $(SRC_DIR)/kernel.cl
	cp $< $@

KERNEL_SRCS := kernel.cl

OPTS ?= -n32

include ../common.mk




