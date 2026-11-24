# Event Sourcing & Chaos Lab

FlowForge'ın SQLite WAL logu ve Chaos Engine'i, millisaniye hassasiyetinde zaman yolculuğu replikasyonu için kritik birleştirilmiş bir aritmetik sistemdir. Altı temel değişmezlik (invariant) şunlardır: 1) Her düğümde atomik "commit" işareti; 2) Süreç ID'leri global dizinde tekil tutulması; 3) Sonsuz döngü için eksponansiyel geriye çekme sınırları; 4) DLQ'den tekrar başlatma öncesi node health checki; 5) Split-brain'i önlemek için merkezi "leader" lock mekanizması; 6) Her subprocess'un ölümüyle eş zamanlı olarak
