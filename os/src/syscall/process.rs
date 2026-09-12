//! Process management syscalls
use crate::{
    task::{current_syscall_count, exit_current_and_run_next, suspend_current_and_run_next},
    timer::get_time_us,
};

#[repr(C)]
#[derive(Debug)]
pub struct TimeVal {
    pub sec: usize,
    pub usec: usize,
}

/// task exits and submit an exit code
pub fn sys_exit(exit_code: i32) -> ! {
    trace!("[kernel] Application exited with code {}", exit_code);
    exit_current_and_run_next();
    panic!("Unreachable in sys_exit!");
}

/// current task gives up resources for other tasks
pub fn sys_yield() -> isize {
    trace!("kernel: sys_yield");
    suspend_current_and_run_next();
    0
}

/// get time with second and microsecond
pub fn sys_get_time(ts: *mut TimeVal, _tz: usize) -> isize {
    trace!("kernel: sys_get_time");
    let us = get_time_us();
    unsafe {
        *ts = TimeVal {
            sec: us / 1_000_000,
            usec: us % 1_000_000,
        };
    }
    0
}

/// 读写当前任务的一个字节，或查询该任务的系统调用次数。
pub fn sys_trace(trace_request: usize, id: usize, data: usize) -> isize {
    trace!("kernel: sys_trace");
    match trace_request {
        // ch3 未启用分页，直接使用用户传入的地址读取一个无符号字节。
        0 => unsafe { (id as *const u8).read_volatile() as isize },
        1 => {
            // 仅写入 data 的最低一个字节，按题目要求不检查地址。
            unsafe { (id as *mut u8).write_volatile(data as u8) };
            0
        }
        // 系统调用入口已计入本次调用，此处只读取计数。
        2 => current_syscall_count(id) as isize,
        // 无效请求不读写内存。
        _ => -1,
    }
}
