# Lab 1：多道程序与分时多任务

## 功能总结

本实验实现了编号为410的sys_trace，支持读取指定地址的无符号字节、写入数据的最低字节，以及查询当前任务的系统调用次数。每个任务使用独立的定长数组计数，系统调用分发前统一更新，包含本次查询。无效请求返回-1，超范围编号查询返回0。已通过trace、sleep、yield测试，并验证字节截取和任务间计数独立性。

## 简答作业

### 1. U态下运行三个bad测例的行为

实验使用仓库中的 `bootloader/rustsbi-qemu.bin`，在QEMU的RISC-V virt平台运行。启动日志显示：RustSBI版本为 **0.3.0-alpha.2**，平台实现为 **RustSBI-QEMU 0.2.0-alpha.2**，适配的SBI规范版本为 **1.0.0**。这三个版本号分别描述RustSBI库、平台实现和接口规范。

本次加载了ch2基础测例和ch3测例，配置可通过 `make -C os run OFFLINE=1 BASE=2` 复现。另使用QEMU的 `-d int` 日志核对了异常号，结果如下。

| 测例 | 执行的操作 | 实际异常与程序表现 |
| --- | --- | --- |
| `ch2b_bad_address.rs` | 向地址0写入一个字节 | 触发Store/AMO access fault，异常号为7，故障地址为0。内核输出 `PageFault in application` 和 `kernel killed it`，终止该任务。 |
| `ch2b_bad_instructions.rs` | 在U态执行S态特权指令 `sret` | 触发Illegal instruction，异常号为2。内核输出 `IllegalInstruction in application, kernel killed it.`，终止该任务。 |
| `ch2b_bad_register.rs` | 在U态读取S态CSR `sstatus` | 触发Illegal instruction，异常号为2，内核输出与上一测例相同，并终止该任务。 |

bad-address测例的输出虽然写着 `PageFault`，实际是访问异常：本章尚未启用分页，[异常处理代码](../os/src/trap/mod.rs)对StoreFault和StorePageFault使用了同一条提示，不能仅凭提示判断为缺页异常。

三个任务均未执行到违规操作之后的失败提示，内核会继续调度其他任务，后续trace、sleep和yield测试正常完成。最后的 `All applications completed!` 是调度器在全部任务结束后主动触发的退出路径。

### 2. 理解陷阱入口与返回过程

[trap.S](../os/src/trap/trap.S)中的 `__alltraps` 在硬件进入S态后切换到内核栈，保存用户寄存器及陷阱状态，调用Rust陷阱处理函数；`__restore` 从内核栈中的TrapContext恢复状态，切回用户栈，并返回用户态。

#### 2.1 刚进入__restore时，sp是什么？有哪些使用情景？

此时 `sp` 指向当前待恢复任务的 **TrapContext起始地址**，该结构位于任务内核栈顶部下方。本实现包含34个八字节槽位，因此该地址为内核栈顶减272字节。

两种情景分别是：

1. **从陷阱处理返回用户程序。** 系统调用或时钟中断处理结束，恢复此前由 `__alltraps` 保存的上下文；若发生任务切换，要等该任务再次获得执行机会后才继续返回。
2. **首次运行用户程序。** 内核预先构造TrapContext，设置用户入口、用户栈指针和U态标志；[TaskContext初始化](../os/src/task/context.rs)将恢复位置设为 `__restore`，由 `__switch` 恢复相应内核栈指针并跳入该入口。

#### 2.2 L43—L48特殊处理了哪些寄存器？

这几行借用 `t0`、`t1`、`t2` 作为临时寄存器，从TrapContext取出以下状态；它们自身的用户态值稍后再恢复。

| 状态 | 来源及作用 |
| --- | --- |
| `sstatus` | 来自第32号槽位。其SPP字段决定陷阱返回后的特权级，恢复为0才能进入U态；SPIE保存的中断状态会在返回时用于恢复SIE。 |
| `sepc` | 来自第33号槽位，决定返回用户态后的执行地址。首次运行时是程序入口；系统调用返回时，内核已将保存的地址加4，跳过原来的 `ecall`；时钟中断返回时则继续被中断的执行流。 |
| 用户 `sp` 与 `sscratch` | 第2号槽位保存用户栈指针，先将其写入 `sscratch` 暂存，保留当前内核栈指针供后续恢复操作使用，直到L60再交换。 |

#### 2.3 L50—L56为何跳过x2和x4？

`x2` 就是 `sp`。恢复其他寄存器时还要通过它访问内核栈上的TrapContext，不能提前替换为用户栈指针，所以使用上一题所述的暂存、交换方式单独恢复。

`x4` 就是线程指针 `tp`。本章假设用户程序不使用它，`__alltraps` 没有保存它，因而此处也不恢复。这是本章实现的简化，后续若使用线程局部存储等功能，需要相应管理它。

#### 2.4 L60执行后，sp和sscratch分别有什么意义？

L58先释放272字节的TrapContext空间，使 `sp` 回到当前任务的内核栈顶。L60交换后，`sp` 恢复为用户栈指针；`sscratch` 保存该任务的内核栈顶，供下一次从用户态陷入时使用。此时处理器仍处于S态。

#### 2.5 __restore在哪条指令切换状态？为何进入U态？

在L61的 `sret` 指令处切换特权级。它根据此前恢复的 `sstatus.SPP` 选择返回模式；本实验中该位为0，因此进入U态，并将PC设为 `sepc`。首次运行时该位由[TrapContext初始化](../os/src/trap/context.rs)设为User；从U态陷入时则由硬件记录为0。返回时还会将SPIE复制给SIE，并将SPIE置1。参见[RISC-V特权规范中的sstatus说明](https://docs.riscv.org/reference/isa/v20260120/priv/supervisor.html#_supervisor_status_sstatus_register)。

#### 2.6 L13执行后，sp和sscratch分别有什么意义？

刚陷入时，硬件已经进入S态，但尚未自动切换栈：`sp` 仍是用户栈指针，`sscratch` 保存此前准备的内核栈顶。L13交换后，`sp` 指向当前任务的内核栈顶，`sscratch` 暂存陷入前的用户栈指针，随后将其保存到TrapContext的第2号槽位。

#### 2.7 从U态进入S态由哪条指令触发？

在系统调用路径中，由用户程序执行 `ecall` 触发Environment call from U-mode异常。该异常被委托给S态处理，硬件记录异常状态、切换特权级并跳到 `stvec` 指定的 `__alltraps`，因此执行L13之前就已处于S态。参见[RISC-V规范中的ECALL说明](https://docs.riscv.org/reference/isa/v20260120/priv/machine.html#_environment_call_and_breakpoint)。

时钟中断以及非法指令、访存异常也能触发从U态进入S态，不要求用户主动执行 `ecall`。`__alltraps` 中的栈交换指令负责切换栈，不负责提升特权级。
