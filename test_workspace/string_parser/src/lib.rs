use core_util::format_id;

pub fn tokenize(input: &str) -> Vec<String> {
    input
        .split_whitespace()
        .map(|s| s.trim_matches(|c: char| !c.is_alphanumeric()).to_lowercase())
        .filter(|s| !s.is_empty())
        .collect()
}

pub fn summarize_tokens(tokens: &[String]) -> String {
    let count = tokens.len() as u64;
    format!("{}: {} tokens", format_id(count), count)
}
