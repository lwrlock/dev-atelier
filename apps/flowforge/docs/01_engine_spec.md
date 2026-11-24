# DAG Compiler & Scheduler

Tarjan algoritması ile SCC'leri tespit edip kritik yol (ES/EF/LS/LF) hesaplanır; döngü varsa sabitlenir. İşçi kiralama için kalp atışı ve süresi dolunca yeniden dizilme mekanizması uygulanır. Hata durumunda üstel geri çekme ve devre kesici durumları yönetilir. Saga deseninde ters topolojik sırayla ödemeler yapılır; bu yapı, bağımlılıkları koruyarak iş akışını optimize eder.
