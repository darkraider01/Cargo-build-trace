pub fn compute_checksum(data: &[u8]) -> u64 {
    let mut sum: u64 = 0x12345678;
    for &b in data {
        sum = sum.rotate_left(5) ^ (b as u64);
    }
    sum
}

pub fn format_id(id: u64) -> String {
    format!("ID-{:016x}", id)
}
