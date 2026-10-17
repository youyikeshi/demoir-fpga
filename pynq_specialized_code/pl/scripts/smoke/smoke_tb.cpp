// 冒烟测试的测试台。
#include <cstdio>

int add(int a, int b);

int main() {
    const int r = add(2, 3);
    if (r != 5) {
        printf("FAIL: add(2,3) = %d\n", r);
        return 1;
    }
    printf("PASS\n");
    return 0;
}
