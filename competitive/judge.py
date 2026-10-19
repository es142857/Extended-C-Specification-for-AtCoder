import math, sys

EPS = 1e-6 # max precision error
f1 = "out.txt"
f2 = "expected_out.txt"

def is_similar(a, b):
    is_int_a = is_int_b = True
    is_num_a = is_num_b = True
    ints = "1234567890"
    nums = "1234567890."

    for i in a:
        if i not in ints: is_int_a = False
        if i not in nums: is_num_a = False
    for i in b:
        if i not in ints: is_int_b = False
        if i not in nums: is_num_b = False
    
    if is_int_b: return a == b
    elif is_num_a and is_num_b:
        return math.isclose(float(a), float(b), abs_tol = EPS) or \
            math.isclose(float(a), float(b), rel_tol = EPS)
    else: return a == b

def is_similar_lis(a, b):
    if len(a) != len(b): return False
    for i in range(len(a)):
        if not is_similar(a[i], b[i]):
            return False
    return True

def stats(name):
    result = [[]]
    with open(name, "r") as f:
        lines = f.readlines()
        for i in lines:
            for j in i.split():
                if j == "":
                    continue
                if j == "==========":
                    result.append([])
                else:
                    result[-1].append(j)
    return result

s1 = stats(f1)
s2 = stats(f2)

if len(s1) != len(s2):
    print("output amount wrong")
    raise SystemExit(1)

cnt_ac = 0
cnt_wa = 0
for i in range(1, len(s1) - 1):
    ac = is_similar_lis(s1[i], s2[i])
    print("Testcase " + str(i) + ": ", end = "")
    if ac: cnt_ac += 1; print("[AC]")
    else: cnt_wa += 1; print("[WA]")

print("Final verdict")
if cnt_ac == 0: print("[WA] x", cnt_wa)
elif cnt_wa == 0: print("[AC] x", cnt_ac)
else: print("[AC] x", str(cnt_ac) + ",", "[WA] x", cnt_wa)

raise SystemExit(0 if cnt_ac > 0 and cnt_wa == 0 else 1)
