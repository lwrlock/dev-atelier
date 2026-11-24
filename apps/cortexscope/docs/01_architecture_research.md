## System Architecture
CortexScope, Ollama'ya doğrudan WebSocket üzerinden bağlanarak TTFT'i etkilemeden anlık akış yakalar. ITL jitteri ve enerji tüketimi için `Joules = (ITL * avg_TTFT) / 100` formülünü uygular. Veritabanı şeması şu tabloları içerir: `runs` (test oturumları), `token_events` (her token'ın zamanlaması ve maliyeti), `arena_matches` (benzersiz performans karşılaştırmaları) ve `hw_samples` (RAPL/xe-GT sıcaklık verileri).

## Edge Cases & Invariants
CortexScope'da Intel RAPL enerji kaydı döngüsü (wraparound), model eviclenmesi, SQLite WAL çakışması ve SSRF gibi kritik senaryolarla karşılaşılmaktadır. Döngü sorununu önlemek için zaman damgalarını normalize etmeniz gerekir. Bağlantı kopmalarını yakalamak için retry mekanizmaları ekleyin. Model eviclenmesini engellemek için bellek sınırlamaları ayarlayın. SQLite çakışmalarını ortadan kaldırmak için WAL modunu synchronous'a çevirin. SSRF saldırılarını durdurmak için upstream URL'leri whitelist ile kontrol edin.

