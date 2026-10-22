use core_util::compute_checksum;

pub fn calculate_factors(val: u64) -> Vec<u64> {
    let mut factors = Vec::new();
    let mut d = 2;
    let mut n = val;
    while d * d <= n {
        if n % d == 0 {
            factors.push(d);
            n /= d;
        } else {
            d += 1;
        }
    }
    if n > 1 {
        factors.push(n);
    }
    factors
}

pub fn hash_of_factors(val: u64) -> u64 {
    let f = calculate_factors(val);
    let bytes: Vec<u8> = f.iter().flat_map(|x| x.to_le_bytes()).collect();
    compute_checksum(&bytes)
}
