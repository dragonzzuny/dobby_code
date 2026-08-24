# Gates

- [x] SUM: add() returns the sum
  CHECK: "C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe" -c "import calc; assert calc.add(3,4)==7; print('SUM-OK')"
  EXPECT: SUM-OK
- [x] IMPORTS: the module imports cleanly
  CHECK: "C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe" -c "import calc; print('IMPORT-OK')"
  EXPECT: IMPORT-OK
- [ ] PRODUCT: mul() returns the product
  CHECK: "C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe" -c "import calc; assert calc.mul(3,4)==12; print('MUL-OK')"
  EXPECT: MUL-OK
