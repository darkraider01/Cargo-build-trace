use orchestrator::process_input;

fn main() {
    let (summary, score) = process_input("Fast incremental compilation in Cargo");
    println!("App result: {} (score: {})", summary, score);
}
