//! Process management syscalls
use crate::config::PAGE_SIZE;
use crate::mm::{translated_byte_buffer, MapPermission, PTEFlags, PageTable, VirtAddr};
use crate::task::{
    change_program_brk, current_syscall_count, current_user_token, exit_current_and_run_next,
    mmap_current_task, munmap_current_task, suspend_current_and_run_next,
};
use crate::timer::get_time_us;
use core::mem::size_of;

#[repr(C)]
#[derive(Debug)]
pub struct TimeVal {
    pub sec: usize,
    pub usec: usize,
}

/// task exits and submit an exit code
pub fn sys_exit(_exit_code: i32) -> ! {
    trace!("kernel: sys_exit");
    exit_current_and_run_next();
    panic!("Unreachable in sys_exit!");
}

/// current task gives up resources for other tasks
pub fn sys_yield() -> isize {
    trace!("kernel: sys_yield");
    suspend_current_and_run_next();
    0
}

/// 获取时间，并按用户页表分段写回可能跨页的 TimeVal。
pub fn sys_get_time(ts: *mut TimeVal, _tz: usize) -> isize {
    trace!("kernel: sys_get_time");
    let token = current_user_token();
    let page_table = PageTable::from_token(token);
    let len = size_of::<TimeVal>();
    let mut start = ts as usize;
    let end = match start.checked_add(len) {
        Some(end) => end,
        None => return -1,
    };
    // 先检查整个缓冲区，避免后续页不可写时只写入一部分时间数据。
    while start < end {
        if page_table.translate_user(start, PTEFlags::W).is_none() {
            return -1;
        }
        start += (PAGE_SIZE - VirtAddr::from(start).page_offset()).min(end - start);
    }

    let us = get_time_us();
    let time = TimeVal {
        sec: us / 1_000_000,
        usec: us % 1_000_000,
    };
    // TimeVal 的两个 usize 字段连续且无填充，以字节切片复制可避免对齐要求。
    let bytes = unsafe { core::slice::from_raw_parts(&time as *const TimeVal as *const u8, len) };
    let mut offset = 0;
    for buffer in translated_byte_buffer(token, ts as *const u8, len) {
        // 虚拟地址连续的各段可落在不连续的物理页中，逐段复制即可。
        let next = offset + buffer.len();
        buffer.copy_from_slice(&bytes[offset..next]);
        offset = next;
    }
    0
}

/// 按用户页表权限读写一个字节，或查询当前任务的系统调用次数。
pub fn sys_trace(trace_request: usize, id: usize, data: usize) -> isize {
    trace!("kernel: sys_trace");
    match trace_request {
        0 | 1 => {
            let page_table = PageTable::from_token(current_user_token());
            let permission = if trace_request == 0 {
                PTEFlags::R
            } else {
                PTEFlags::W
            };
            // 内核通过恒等映射访问物理内存，必须先检查原用户页表的权限。
            let pa = match page_table.translate_user(id, permission) {
                Some(pa) => pa,
                None => return -1,
            };
            if trace_request == 0 {
                unsafe { (pa.0 as *const u8).read_volatile() as isize }
            } else {
                // 仅写入最低一个字节，不修改相邻内存。
                unsafe { (pa.0 as *mut u8).write_volatile(data as u8) };
                0
            }
        }
        // 系统调用入口已计入本次调用，此处只查询。
        2 => current_syscall_count(id) as isize,
        _ => -1,
    }
}

/// 按指定权限申请匿名映射，参数错误、映射冲突或物理页不足时返回 -1。
pub fn sys_mmap(start: usize, len: usize, prot: usize) -> isize {
    trace!("kernel: sys_mmap");
    // 起始地址必须页对齐，权限仅允许非零的 R/W/X 三位。
    if start % PAGE_SIZE != 0 || prot == 0 || prot & !0x7 != 0 {
        return -1;
    }
    if len == 0 {
        return 0;
    }
    // 结束地址向上按页对齐，并检查计算溢出。
    let end = match start
        .checked_add(len)
        .and_then(|end| end.checked_add(PAGE_SIZE - 1))
    {
        Some(end) => end & !(PAGE_SIZE - 1),
        None => return -1,
    };
    let start_va = VirtAddr::from(start);
    let end_va = VirtAddr::from(end);
    // 防止 VirtAddr 截断高位后把无效区间映射到其他地址。
    if usize::from(start_va) != start || usize::from(end_va) != end || start_va >= end_va {
        return -1;
    }
    // prot 的 R/W/X 比页表权限低一位；用户映射还必须加上 U 位。
    let permission = MapPermission::from_bits((prot as u8) << 1).unwrap() | MapPermission::U;
    mmap_current_task(start_va, end_va, permission).map_or(-1, |_| 0)
}

/// 取消指定区间的映射，长度由任务层按页向上取整。
pub fn sys_munmap(start: usize, len: usize) -> isize {
    trace!("kernel: sys_munmap");
    // 保留测例要求的页对齐检查；空区间不需要解除任何映射。
    if start % PAGE_SIZE != 0 {
        return -1;
    }
    if len == 0 {
        return 0;
    }
    let end = match start.checked_add(len) {
        Some(end) => end,
        None => return -1,
    };
    munmap_current_task(start.into(), end.into()).map_or(-1, |_| 0)
}
/// change data segment size
pub fn sys_sbrk(size: i32) -> isize {
    trace!("kernel: sys_sbrk");
    if let Some(old_brk) = change_program_brk(size) {
        old_brk as isize
    } else {
        -1
    }
}
