/* 直接链 OpenBLAS 静态库测 SGEMM 吞吐 —— 不依赖 numpy、不依赖 .so。
 *
 * 用途：① 证明 TARGET=ARMV7 真的选了哪个 core/kernel；② 单独测 VFP 4x4 内核的
 *       SGEMM 吞吐（numpy 那边还有 im2col/调度开销，这里是最纯的内核性能）；
 * ③ 顺手验一个小 case 的正确性（列主序 Fortran ABI）。
 *
 * 板上编译：
 *   gcc -O2 sgemm_bench.c libopenblas_armv7p-r0.3.30.a -lpthread -lm -o sgemm_bench
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

extern void sgemm_(const char *, const char *, const int *, const int *, const int *,
                   const float *, const float *, const int *, const float *, const int *,
                   const float *, float *, const int *);
/* NO_LAPACK/NOFORTRAN 的静态库里没有 xerbla_，自己给个桩（不影响任何计算） */
void xerbla_(const char *srname, const int *info)
{
    printf("  [xerbla] %s info=%d\n", srname, info ? *info : -1);
}

static double now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

int main(void)
{
    printf("OpenBLAS 0.3.30 静态库 / TARGET=ARMV7 (VFPv3 4x4 内核)\n");

    /* ---- 小 case 正确性（列主序）：A(2x3) B(3x2) ---- */
    {
        int m = 2, n = 2, k = 3;
        float A[6] = {1, 4, 2, 5, 3, 6};      /* col-major 2x3 */
        float B[6] = {7, 9, 11, 8, 10, 12};   /* col-major 3x2 */
        float C[4] = {0, 0, 0, 0};
        float alpha = 1.0f, beta = 0.0f;
        sgemm_("N", "N", &m, &n, &k, &alpha, A, &m, B, &k, &beta, C, &m);
        printf("sanity 2x3x2      : [%g %g; %g %g]  (期望 [58 64; 139 154])\n",
               C[0], C[2], C[1], C[3]);
    }

    /* ---- 吞吐：真实 conv_first 形状 M=48 K=300 N=16384 ---- */
    {
        const int M = 48, K = 300, N = 16384;
        float *A = malloc(sizeof(float) * M * K);
        float *B = malloc(sizeof(float) * K * N);
        float *C = malloc(sizeof(float) * M * N);
        if (!A || !B || !C) { printf("OOM\n"); return 1; }
        for (int i = 0; i < M * K; i++) A[i] = (float)((i % 17) - 8) * 0.01f;
        for (int i = 0; i < K * N; i++) B[i] = (float)((i % 13) - 6) * 0.01f;
        memset(C, 0, sizeof(float) * M * N);

        float alpha = 1.0f, beta = 0.0f;
        double best = 1e9;
        for (int rep = 0; rep < 3; rep++) {
            double t0 = now();
            sgemm_("N", "N", &M, &N, &K, &alpha, A, &M, B, &K, &beta, C, &M);
            double dt = now() - t0;
            if (dt < best) best = dt;
        }
        double fl = 2.0 * M * K * N;
        printf("sgemm 48x300x16384: %.1f ms -> %.2f GFLOP/s  (C[0]=%.6g)\n",
               best * 1e3, fl / best / 1e9, C[0]);
        free(A); free(B); free(C);
    }

    /* ---- 纯内核峰值：大方阵（M=N=256, K=2048），排除边缘开销 ---- */
    {
        const int M = 256, K = 2048, N = 256;
        float *A = malloc(sizeof(float) * M * K);
        float *B = malloc(sizeof(float) * K * N);
        float *C = malloc(sizeof(float) * M * N);
        for (int i = 0; i < M * K; i++) A[i] = 0.001f * (i % 7);
        for (int i = 0; i < K * N; i++) B[i] = 0.001f * (i % 5);
        float alpha = 1.0f, beta = 0.0f;
        double best = 1e9;
        for (int rep = 0; rep < 2; rep++) {
            double t0 = now();
            sgemm_("N", "N", &M, &N, &K, &alpha, A, &M, B, &K, &beta, C, &M);
            double dt = now() - t0;
            if (dt < best) best = dt;
        }
        double fl = 2.0 * M * K * N;
        printf("sgemm 256x2048x256: %.1f ms -> %.2f GFLOP/s  (内核峰值参考)\n",
               best * 1e3, fl / best / 1e9);
    }
    return 0;
}
