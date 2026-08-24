# GATES-CONJUNCTION fixture

The two gates differ only in whether the output decides anything.

- [x] EXIT0: a command that exits 0 and proves nothing
  CHECK: "C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe" -c "print('unrelated output')"
  EXPECT: the-token-that-decides
- [x] HONEST: a command whose output decides the claim
  CHECK: "C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe" -c "print('the-token-that-decides')"
  EXPECT: the-token-that-decides
