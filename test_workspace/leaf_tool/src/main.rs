use string_parser::tokenize;

fn main() {
    let words = tokenize("hello leaf tool world");
    println!("Leaf tool processed {} words", words.len());
}
