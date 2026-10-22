use math_engine::hash_of_factors;
use string_parser::{tokenize, summarize_tokens};

pub fn process_input(text: &str) -> (String, u64) {
    let tokens = tokenize(text);
    let summary = summarize_tokens(&tokens);
    let score = hash_of_factors(tokens.len() as u64 + 42);
    (summary, score)
}
